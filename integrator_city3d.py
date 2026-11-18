#!/usr/bin/env python3
"""Intégrateur city3d : prépare les fichiers de bâtiments LoD2 (.bld) et de canopée (.veg) lus par le module geo-lidar-hd.

    python3 -I integrator_city3d.py download --around 0846-6494 --output site/        # télécharge, reconstruit, convertit
    python3 -I integrator_city3d.py download --tiles 0846-6494 0847-6494 --output site/ --previous-index https://…/index.json
    python3 -I integrator_city3d.py convert  --tiles 0846-6494 --cache cache/ --output site/    # hors ligne, depuis le cache
    python3 -I integrator_city3d.py merge --previous-index https://…/index.json --output site/ partiel1/ partiel2/

Une dalle se nomme « XXXX-YYYY » (coin nord-ouest en km, Lambert-93). `download` lit le nuage COPC de la dalle (champ url_npl du
WFS de l'IGN) et les emprises de la BD TOPO, lance roofer (bâtiments) et PDAL (végétation) dans Docker, puis écrit
`buildings/<dalle>.bld`, `vegetation/<dalle>.veg` et `index.json` dans --output. `merge` rassemble des sorties partielles et reprend
les fichiers déjà publiés pour les dalles non régénérées : un déploiement GitHub Pages remplace tout le site.

Codes de sortie : 0 fichiers générés, 1 erreur, 10 rien à faire (toutes les dalles demandées sont déjà à jour dans l'index précédent).
Bibliothèque standard seulement ; Docker pour roofer et PDAL. Format de sortie : GUIDE-DEV.md du module geo-lidar-hd (source de vérité).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ('buildings', 'vegetation'):   # -I n'ajoute pas le dossier du script au chemin des modules
    sys.path.insert(0, os.path.join(HERE, _sub))
import cityjsonseq_to_bld as bld_conv  # noqa: E402
import pack_veg  # noqa: E402

__version__ = '0.1.0'
FORMAT_VERSION = 1   # version du format de sortie (index.json) : incrémentée pour tout changement incompatible

ROOFER_IMAGE = '3dgi/roofer:v1.0.0'
PDAL_IMAGE = 'pdal/pdal@sha256:3e38c3feaa100878a285a8dba721dea3995f335f2b70e930382586279e32e064'   # PDAL 2.10.2 (testée le 2026-11-13)
TOOLS = {'roofer': ROOFER_IMAGE, 'pdal': PDAL_IMAGE}
WFS = 'https://data.geopf.fr/wfs/ows'
SOURCE = 'IGN, LiDAR HD et BD TOPO®, traitement Vienne Condrieu Agglomération'
LICENCE = 'Licence Ouverte 2.0'
FEATURES = ('buildings', 'vegetation')
TILE_RE = re.compile(r'^(\d{4})-(\d{4})$')
RETRY_STATUS = (429, 500, 502, 503, 504)

EXIT_OK, EXIT_ERROR, EXIT_UP_TO_DATE = 0, 1, 10


class Error(Exception):
    """Erreur attendue : message pour l'utilisateur, code de sortie 1."""


# ---------------------------------------------------------------- dalles

def tile_origin(tile):
    """Coin nord-ouest de la dalle en mètres (Lambert-93)."""
    m = TILE_RE.match(tile)
    if not m:
        raise Error('nom de dalle attendu « XXXX-YYYY » : %r' % tile)
    return int(m.group(1)) * 1000, int(m.group(2)) * 1000


def expand_tiles(tiles, around=None):
    """Liste des dalles demandées : --tiles tel quel, ou --around = la dalle et ses 8 voisines (ligne nord d'abord)."""
    out = []
    if around:
        x, y = tile_origin(around)
        x, y = x // 1000, y // 1000
        out += ['%04d-%04d' % (x + dx, y + dy) for dy in (1, 0, -1) for dx in (-1, 0, 1)]
    for t in tiles or []:
        tile_origin(t)
        out.append(t)
    if not out:
        raise Error('aucune dalle demandée : --tiles XXXX-YYYY… ou --around XXXX-YYYY')
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t))]


# ---------------------------------------------------------------- réseau

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != 'https':
            raise urllib.error.URLError('redirection non https refusée : ' + newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect)


def open_url(url, opener=None, retries=5, sleep=time.sleep, retry_status=RETRY_STATUS):
    """Ouvre une adresse https, avec reprises à délai croissant (429 et erreurs serveur : data.geopf.fr limite le débit)."""
    if urllib.parse.urlparse(url).scheme != 'https':
        raise Error('adresse non https refusée : ' + url)
    request = urllib.request.Request(url, headers={'User-Agent': 'integrator-city3d/' + __version__})
    last = None
    for attempt in range(retries + 1):
        try:
            return (opener or _OPENER).open(request, timeout=120)
        except urllib.error.HTTPError as e:
            last = 'HTTP %d' % e.code
            if e.code not in retry_status or attempt == retries:
                raise Error('%s : %s' % (url, last))
            wait = e.headers.get('Retry-After') if e.headers else None
            delay = float(wait) if wait and wait.replace('.', '', 1).isdigit() else min(2 ** attempt, 60)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(getattr(e, 'reason', e))
            if attempt == retries:
                raise Error('%s : %s' % (url, last))
            delay = min(2 ** attempt, 60)
        sleep(delay)
    raise Error('%s : %s' % (url, last))


def fetch_json(url, tries=4, sleep=time.sleep, **kw):
    """JSON d'une adresse https. Une réponse qui n'en est pas (page d'erreur, corps vide ou tronqué : le WFS de l'IGN en renvoie
    parfois en HTTP 200) est rejouée, puis refusée avec le début de ce qui est arrivé."""
    for attempt in range(tries):
        with open_url(url, sleep=sleep, **kw) as r:
            body = r.read()
        try:
            return json.loads(body)
        except ValueError:
            if attempt == tries - 1:
                raise Error('%s : réponse qui n\'est pas du JSON (%r)' % (url, body[:120]))
            sleep(min(2 ** attempt, 60))


def download_file(url, dest, log, **kw):
    """Télécharge vers `dest` (via un fichier .part) sauf s'il existe déjà : le cache n'est pas retéléchargé."""
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log('téléchargement : ' + url)
    with open_url(url, **kw) as r, open(dest + '.part', 'wb') as out:
        shutil.copyfileobj(r, out, 1 << 20)
    os.replace(dest + '.part', dest)


def lookup_tile(tile, **kw):
    """Nuage COPC de la dalle d'après le WFS des dalles LiDAR HD, interrogé en son centre : {url, edition}."""
    x, y = tile_origin(tile)
    cx, cy = x + 500, y - 500
    query = urllib.parse.urlencode({
        'SERVICE': 'WFS', 'REQUEST': 'GetFeature', 'VERSION': '2.0.0', 'TYPENAMES': 'IGNF_LIDAR-HD_METADONNEE:metadata',
        'OUTPUTFORMAT': 'application/json', 'SRSNAME': 'EPSG:2154', 'COUNT': 1,
        'BBOX': '%d,%d,%d,%d,EPSG:2154' % (cx, cy, cx, cy)}, safe=',:/')
    # le WFS répond parfois 400 à une requête valide qui réussit au coup suivant : on la rejoue aussi
    features = fetch_json(WFS + '?' + query, retry_status=RETRY_STATUS + (400,), **kw).get('features') or []
    if not features:
        raise Error('dalle %s : aucune dalle LiDAR HD ici' % tile)
    props = features[0].get('properties') or {}
    if props.get('coordonnees_nw') != tile:
        raise Error('dalle %s : le WFS renvoie la dalle %s' % (tile, props.get('coordonnees_nw')))
    if not props.get('url_npl'):
        raise Error('dalle %s : pas de fichier COPC (url_npl absent)' % tile)
    return {'url': props['url_npl'], 'edition': str(props.get('date_edition') or '')[:10] or None}


def fetch_bdtopo(tile, dest, log, **kw):
    """Emprises des bâtiments (BD TOPO) de la dalle, en GeoJSON Lambert-93. Une réponse tronquée par le WFS est refusée."""
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return
    x, y = tile_origin(tile)
    query = urllib.parse.urlencode({
        'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetFeature', 'TYPENAMES': 'BDTOPO_V3:batiment',
        'OUTPUTFORMAT': 'application/json', 'SRSNAME': 'EPSG:2154',
        'BBOX': '%d,%d,%d,%d,EPSG:2154' % (x, y - 1000, x + 1000, y)}, safe=',:/')
    log('emprises BD TOPO : dalle ' + tile)
    data = fetch_json(WFS + '?' + query, retry_status=RETRY_STATUS + (400,), **kw)
    n, matched = len(data.get('features') or []), data.get('numberMatched')
    if matched is not None and n != matched:
        raise Error('dalle %s : réponse WFS tronquée (%d bâtiments sur %d), pagination à prévoir' % (tile, n, matched))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest + '.part', 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.replace(dest + '.part', dest)
    log('BD TOPO : %d bâtiments' % n)


# ---------------------------------------------------------------- outils Docker

def docker_run(cmd):
    if shutil.which('docker') is None:
        raise Error('docker introuvable : roofer et PDAL tournent dans des conteneurs')
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise Error('échec de la commande : ' + ' '.join(cmd[:6]) + '… (code %d)' % e.returncode)


def _user():
    return '%d:%d' % (os.getuid(), os.getgid()) if hasattr(os, 'getuid') else '0:0'


def roofer_command(tile, work):
    """roofer sur le COPC et les emprises BD TOPO de la dalle -> work/buildings/out/*.city.jsonl."""
    x, y = tile_origin(tile)
    work = os.path.abspath(work)
    return ['docker', 'run', '--rm', '--user', _user(), '-w', '/data',
            '-v', work + '/buildings:/data', '-v', work + '/copc:/copc:ro', ROOFER_IMAGE,
            '--box', str(x), str(y - 1000), str(x + 1000), str(y), '--id-attribute', 'cleabs',
            '/copc/%s.copc.laz' % tile, '/data/bdtopo.geojson', '/data/out']


def pdal_pipeline(tile, name, limits, stat):
    """Passe PDAL : lecture COPC bornée à la dalle, filtre de classes, grille de 1 m (max ou min de Z). PDAL n'accepte qu'un écrivain par pipeline."""
    x, y = tile_origin(tile)
    return [
        {'type': 'readers.copc', 'filename': '/in/%s.copc.laz' % tile, 'bounds': '([%d, %d], [%d, %d])' % (x, x + 1000, y - 1000, y)},
        {'type': 'filters.range', 'limits': limits},
        {'type': 'writers.gdal', 'filename': '/veg/%s.tif' % name, 'resolution': 1.0, 'output_type': stat, 'origin_x': x,
         'origin_y': y - 1000, 'width': 1000, 'height': 1000, 'data_type': 'float32', 'nodata': -9999, 'dimension': 'Z'}]


def pdal_commands(tile, work, name):
    work = os.path.abspath(work)
    base = ['docker', 'run', '--rm', '--user', _user()]
    return [base + ['-v', work + '/vegetation:/veg', '-v', work + '/copc:/in:ro', PDAL_IMAGE, 'pdal', 'pipeline', '/veg/%s.json' % name],
            base + ['-v', work + '/vegetation:/veg', PDAL_IMAGE, 'gdal_translate', '-q', '-of', 'ENVI', '/veg/%s.tif' % name, '/veg/%s.bin' % name]]


# ---------------------------------------------------------------- une dalle

def _write(path, blob):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.part', 'wb') as fh:
        fh.write(blob)
    os.replace(path + '.part', path)


def build_buildings(tile, work, output, run, log, warnings):
    out = os.path.join(work, 'buildings', 'out')
    jsonl = sorted(os.path.join(out, f) for f in os.listdir(out) if f.endswith('.city.jsonl')) if os.path.isdir(out) else []
    if not jsonl:
        for p in (os.path.join(work, 'copc', tile + '.copc.laz'), os.path.join(work, 'buildings', 'bdtopo.geojson')):
            if not os.path.isfile(p):
                raise Error('dalle %s : %s absent du cache (lancer download)' % (tile, os.path.relpath(p, work)))
        os.makedirs(out, exist_ok=True)
        log('roofer (%s) : dalle %s' % (ROOFER_IMAGE, tile))
        run(roofer_command(tile, work))
        jsonl = sorted(os.path.join(out, f) for f in os.listdir(out) if f.endswith('.city.jsonl'))
        if not jsonl:
            raise Error('dalle %s : roofer n\'a produit aucune sortie' % tile)
    msgs = []
    try:
        data, st = bld_conv.convert(jsonl, tile, '2.2', log=msgs.append)
    except ValueError as e:
        raise Error('dalle %s : %s' % (tile, e))
    blob = bld_conv.encode(data)
    path = os.path.join(output, 'buildings', tile + '.bld')
    _write(path, blob)
    for m in msgs:
        log('avertissement : ' + m)
    if st.no_ground:
        warnings.append('dalle %s : %d bâtiments sans GroundSurface (ni pied de mur ni emprise)' % (tile, st.no_ground))
    if st.bad_faces or st.degraded or st.area_mismatch:
        warnings.append('dalle %s : faces non triangulées %d, triangulation dégradée %d, aire discordante %d'
                        % (tile, st.bad_faces, st.degraded, st.area_mismatch))
    log('%s : %d bâtiments, %d triangles, %d octets' % (os.path.basename(path), st.buildings, len(data['indices']) // 3, len(blob)))
    return {'fichier': 'buildings/%s.bld' % tile, 'taille': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(), 'nombre': st.buildings}


def build_vegetation(tile, work, output, run, log, warnings):
    veg = os.path.join(work, 'vegetation')
    for name, limits, stat in (('top', 'Classification[3:5]', 'max'), ('base', 'Classification[5:5]', 'min')):
        bin_path = os.path.join(veg, name + '.bin')
        if os.path.isfile(bin_path) and os.path.getsize(bin_path) > 0:
            continue
        copc = os.path.join(work, 'copc', tile + '.copc.laz')
        if not os.path.isfile(copc):
            raise Error('dalle %s : copc/%s.copc.laz absent du cache (lancer download)' % (tile, tile))
        os.makedirs(veg, exist_ok=True)
        with open(os.path.join(veg, name + '.json'), 'w', encoding='utf-8') as fh:
            json.dump(pdal_pipeline(tile, name, limits, stat), fh, indent=2)
        log('PDAL (%s) : dalle %s, bande %s' % (name, tile, name))
        for cmd in pdal_commands(tile, work, name):
            run(cmd)
    x0, y1 = tile_origin(tile)
    size = 1000
    try:
        top, base = pack_veg.read_grid(os.path.join(veg, 'top.bin'), size), pack_veg.read_grid(os.path.join(veg, 'base.bin'), size)
    except (SystemExit, OSError) as e:
        raise Error('dalle %s : grille PDAL illisible (%s)' % (tile, e))
    blob = pack_veg.pack([top, base], size, 1.0, (float(x0), float(y1)))
    path = os.path.join(output, 'vegetation', tile + '.veg')
    _write(path, blob)
    n_top = sum(v is not None for v in top)
    if n_top == 0:
        warnings.append('dalle %s : aucune cellule de végétation (classes 3 à 5)' % tile)
    log('%s : %d cellules avec sommet, %d octets' % (os.path.basename(path), n_top, len(blob)))
    return {'fichier': 'vegetation/%s.veg' % tile, 'taille': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}


def process_tile(tile, cache, output, features, run, log, warnings, source=None):
    work = os.path.join(cache, tile)
    entry = {'integrateur': __version__, 'outils': dict(TOOLS), 'source': source}
    if 'buildings' in features:
        entry['batiments'] = build_buildings(tile, work, output, run, log, warnings)
    if 'vegetation' in features:
        entry['vegetation'] = build_vegetation(tile, work, output, run, log, warnings)
    return entry


# ---------------------------------------------------------------- index

def new_index(dalles):
    return {'format': FORMAT_VERSION, 'source': SOURCE, 'licence': LICENCE, 'dalles': dict(sorted(dalles.items()))}


def write_index(output, index):
    os.makedirs(output, exist_ok=True)
    with open(os.path.join(output, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
        fh.write('\n')


def read_index(location):
    """Index déjà publié : fichier local ou adresse https. Retourne (index, base) où base préfixe les fichiers qu'il décrit."""
    if urllib.parse.urlparse(location).scheme == 'https':
        return fetch_json(location), location.rsplit('/', 1)[0]
    with open(location, encoding='utf-8') as fh:
        return json.load(fh), os.path.dirname(os.path.abspath(location))


def load_previous(location, log):
    """Index précédent, ou None s'il est absent ou illisible (première exécution) : tout est alors régénéré."""
    if not location:
        return None
    try:
        index, _ = read_index(location)
    except (Error, OSError, ValueError) as e:
        log('aucun index précédent exploitable (%s) : toutes les dalles seront générées' % e)
        return None
    if not isinstance(index, dict) or index.get('format') != FORMAT_VERSION:
        log('index précédent d\'un autre format : toutes les dalles seront générées')
        return None
    return index


def is_current(previous, tile, source, features):
    """Dalle à jour : même version de l'intégrateur, mêmes outils, même édition du nuage, mêmes fonctions déjà produites."""
    entry = (previous or {}).get('dalles', {}).get(tile)
    if not entry or entry.get('integrateur') != __version__ or entry.get('outils') != TOOLS:
        return False
    if not source or not source.get('edition') or (entry.get('source') or {}).get('edition') != source['edition']:
        return False
    return all(entry.get({'buildings': 'batiments', 'vegetation': 'vegetation'}[f]) for f in features)


# ---------------------------------------------------------------- commandes

def default_cache():
    return os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'integrator-city3d')


def cmd_download(args, run=docker_run, log=None):
    log = log or (lambda m: sys.stderr.write(m + '\n'))
    tiles = expand_tiles(args.tiles, args.around)
    features = args.features or list(FEATURES)
    previous = load_previous(args.previous_index, log)
    produced, warnings, current = {}, [], []
    for tile in tiles:
        log('=== dalle %s ===' % tile)
        source = lookup_tile(tile)
        if previous and not args.force and is_current(previous, tile, source, features):
            log('déjà à jour dans l\'index précédent : ignorée')
            current.append(tile)
            continue
        work = os.path.join(args.cache, tile)
        download_file(source['url'], os.path.join(work, 'copc', tile + '.copc.laz'), log)
        if 'buildings' in features:
            fetch_bdtopo(tile, os.path.join(work, 'buildings', 'bdtopo.geojson'), log)
        produced[tile] = process_tile(tile, args.cache, args.output, features, run, log, warnings, source)
        if args.purge:
            shutil.rmtree(work, ignore_errors=True)
    if not produced:
        log('rien à générer : %d dalle(s) déjà à jour' % len(current))
        return EXIT_UP_TO_DATE
    write_index(args.output, new_index(produced))
    return _finish(warnings, args.strict, log, '%d dalle(s) générée(s), %d déjà à jour' % (len(produced), len(current)))


def cmd_convert(args, run=docker_run, log=None):
    log = log or (lambda m: sys.stderr.write(m + '\n'))
    tiles = expand_tiles(args.tiles, args.around)
    features = args.features or list(FEATURES)
    produced, warnings = {}, []
    for tile in tiles:
        log('=== dalle %s ===' % tile)
        produced[tile] = process_tile(tile, args.cache, args.output, features, run, log, warnings, None)
    write_index(args.output, new_index(produced))
    return _finish(warnings, args.strict, log, '%d dalle(s) convertie(s)' % len(produced))


def _finish(warnings, strict, log, summary):
    for w in warnings:
        log('AVERTISSEMENT : ' + w)
    log(summary + (' ; %d avertissement(s)' % len(warnings) if warnings else ''))
    if warnings and strict:
        log('--strict : exécution en échec à cause des avertissements')
        return EXIT_ERROR
    return EXIT_OK


def _read_bytes(base, rel):
    if urllib.parse.urlparse(base).scheme == 'https':
        with open_url(base + '/' + rel) as r:
            return r.read()
    with open(os.path.join(base, rel), 'rb') as fh:
        return fh.read()


def cmd_merge(args, log=None):
    """Sorties partielles + dalles déjà publiées -> un site complet. Toute dalle reprise est contrôlée (taille et sha256)."""
    log = log or (lambda m: sys.stderr.write(m + '\n'))
    if os.path.isdir(args.output) and os.listdir(args.output):
        raise Error('%s n\'est pas vide : choisir un dossier de sortie neuf' % args.output)
    dalles, sources = {}, {}
    previous, prev_base = None, None
    if args.previous_index:
        try:
            previous, prev_base = read_index(args.previous_index)
        except (Error, OSError, ValueError) as e:
            log('aucun index précédent exploitable (%s)' % e)
    if previous is not None:
        if previous.get('format') != FORMAT_VERSION:
            raise Error('l\'index précédent est au format %r, pas %d : régénérer toutes les dalles' % (previous.get('format'), FORMAT_VERSION))
        for tile, entry in previous.get('dalles', {}).items():
            dalles[tile], sources[tile] = entry, prev_base
    for part in args.partials:
        index, _ = read_index(os.path.join(part, 'index.json'))
        if index.get('format') != FORMAT_VERSION:
            raise Error('%s : format %r, attendu %d' % (part, index.get('format'), FORMAT_VERSION))
        for tile, entry in index.get('dalles', {}).items():
            dalles[tile], sources[tile] = entry, os.path.abspath(part)
    if not dalles:
        raise Error('rien à rassembler')
    for tile in sorted(dalles):
        for key in ('batiments', 'vegetation'):
            item = dalles[tile].get(key)
            if not item:
                continue
            blob = _read_bytes(sources[tile], item['fichier'])
            if len(blob) != item['taille'] or hashlib.sha256(blob).hexdigest() != item['sha256']:
                raise Error('dalle %s : %s ne correspond pas à son index (taille ou sha256)' % (tile, item['fichier']))
            _write(os.path.join(args.output, item['fichier']), blob)
    write_index(args.output, new_index(dalles))
    log('%d dalle(s) dans %s' % (len(dalles), args.output))
    return EXIT_OK


# ---------------------------------------------------------------- ligne de commande

def build_parser():
    ap = argparse.ArgumentParser(prog='integrator_city3d.py', description=__doc__.split('\n')[0])
    ap.add_argument('--version', action='version', version='integrator-city3d ' + __version__)
    sub = ap.add_subparsers(dest='command', required=True)

    def common(p):
        p.add_argument('--tiles', nargs='+', metavar='XXXX-YYYY', help='dalles demandées')
        p.add_argument('--around', metavar='XXXX-YYYY', help='la dalle et ses 8 voisines')
        p.add_argument('--output', required=True, help='dossier de sortie (buildings/, vegetation/, index.json)')
        p.add_argument('--cache', default=default_cache(), help='dossier de travail par dalle (défaut : %(default)s)')
        p.add_argument('--features', nargs='+', choices=FEATURES, help='fonctions à préparer (défaut : toutes)')
        p.add_argument('--strict', action='store_true', help='échec s\'il y a des avertissements')

    d = sub.add_parser('download', help='télécharger, reconstruire et convertir')
    common(d)
    d.add_argument('--previous-index', help='index.json déjà publié (fichier ou adresse https) : les dalles à jour sont ignorées')
    d.add_argument('--force', action='store_true', help='régénérer même si la dalle est à jour')
    d.add_argument('--purge', action='store_true', help='supprimer le dossier de travail de chaque dalle une fois traitée (disque limité)')
    c = sub.add_parser('convert', help='convertir depuis le cache, sans accès au réseau')
    common(c)
    m = sub.add_parser('merge', help='rassembler des sorties partielles et les dalles déjà publiées')
    m.add_argument('partials', nargs='*', metavar='DOSSIER', help='sorties partielles (chacune avec son index.json)')
    m.add_argument('--previous-index', help='index.json déjà publié (fichier ou adresse https)')
    m.add_argument('--output', required=True, help='dossier de sortie neuf')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'download':
            return cmd_download(args)
        if args.command == 'convert':
            return cmd_convert(args)
        return cmd_merge(args)
    except Error as e:
        sys.stderr.write('erreur : %s\n' % e)
        return EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main())
