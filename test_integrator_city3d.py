"""Tests de la ligne de commande : python3 -I -m unittest discover -s . -p 'test_integrator*.py'   (sans Docker ni réseau)"""
import array
import email.message
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

import integrator_city3d as ic


class _Once(object):
    """open() dont read() et write() referment le fichier aussitôt (pas de fichier laissé ouvert dans les tests)."""
    def __init__(self, *a, **k):
        self.f = open(*a, **k)

    def read(self):
        with self.f:
            return self.f.read()

    def write(self, data):
        with self.f:
            return self.f.write(data)



HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
TILE = '0846-6494'
SOURCE = {'url': 'https://data.geopf.fr/telechargement/x/LHD_FXX_0846_6494_PTS_LAMB93_IGN69.copc.laz', 'edition': '2025-02-20'}


def fake_grid(path, value):
    """Grille ENVI float32 de 1000 x 1000 cellules : -9999 partout sauf un carré de 20 x 20 cellules."""
    g = array.array('f', [-9999.0] * 1000000)
    for r in range(100, 120):
        for c in range(100, 120):
            g[r * 1000 + c] = value
    with open(path, 'wb') as fh:
        fh.write(g.tobytes())


class FakeDocker(object):
    """Remplace Docker : roofer recopie la fausse sortie de la fixture, gdal_translate écrit une grille. Garde les commandes."""
    def __init__(self):
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == '-v']
        if ic.ROOFER_IMAGE in cmd:
            data = [m.split(':')[0] for m in mounts if m.endswith(':/data')][0]
            os.makedirs(os.path.join(data, 'out'), exist_ok=True)
            shutil.copy(os.path.join(FIX, 'fake.city.jsonl'), os.path.join(data, 'out', '846000_6493000.city.jsonl'))
        elif 'gdal_translate' in cmd:
            veg = [m.split(':')[0] for m in mounts if m.endswith(':/veg')][0]
            name = os.path.basename(cmd[-1])
            fake_grid(os.path.join(veg, name), 110.0 if name == 'top.bin' else 102.0)


class Args(object):
    def __init__(self, **kw):
        self.tiles, self.around, self.features, self.previous_index = None, None, None, None
        self.force, self.purge, self.strict = False, False, False
        self.__dict__.update(kw)


class Tiles(unittest.TestCase):
    def test_origin_and_around(self):
        self.assertEqual(ic.tile_origin(TILE), (846000, 6494000))
        around = ic.expand_tiles(None, TILE)
        self.assertEqual(len(around), 9)
        self.assertEqual(around[0], '0845-6495')
        self.assertEqual(around[4], TILE)
        self.assertEqual(around[8], '0847-6493')

    def test_explicit_list_without_duplicates(self):
        self.assertEqual(ic.expand_tiles([TILE, '0847-6494', TILE]), [TILE, '0847-6494'])

    def test_invalid_names_and_empty_request(self):
        for bad in ('846-6494', 'abcd-1234', '0846_6494', ''):
            with self.assertRaises(ic.Error):
                ic.tile_origin(bad)
        with self.assertRaises(ic.Error):
            ic.expand_tiles(None, None)


class Docker(unittest.TestCase):
    def test_roofer_command(self):
        cmd = ic.roofer_command(TILE, '/tmp/w')
        self.assertEqual(cmd[:3], ['docker', 'run', '--rm'])
        self.assertIn(ic.ROOFER_IMAGE, cmd)
        self.assertEqual(cmd[cmd.index('--box') + 1:cmd.index('--box') + 5], ['846000', '6493000', '847000', '6494000'])
        self.assertIn('/tmp/w/buildings:/data', cmd)
        self.assertIn('/tmp/w/copc:/copc:ro', cmd)   # le nuage est monté en lecture seule
        self.assertEqual(cmd[-3:], ['/copc/%s.copc.laz' % TILE, '/data/bdtopo.geojson', '/data/out'])
        self.assertEqual(cmd[cmd.index('--id-attribute') + 1], 'cleabs')

    def test_pdal_pipeline_and_commands(self):
        top = ic.pdal_pipeline(TILE, 'top', 'Classification[3:5]', 'max')
        self.assertEqual(top[0]['bounds'], '([846000, 847000], [6493000, 6494000])')
        self.assertEqual(top[2]['output_type'], 'max')
        self.assertEqual((top[2]['origin_x'], top[2]['origin_y'], top[2]['width'], top[2]['height']), (846000, 6493000, 1000, 1000))
        pdal, translate = ic.pdal_commands(TILE, '/tmp/w', 'top')
        self.assertIn(ic.PDAL_IMAGE, pdal)
        self.assertIn('/tmp/w/copc:/in:ro', pdal)
        self.assertEqual(translate[-4:], ['-of', 'ENVI', '/veg/top.tif', '/veg/top.bin'])

    def test_pinned_images(self):
        self.assertRegex(ic.PDAL_IMAGE, r'@sha256:[0-9a-f]{64}$')   # jamais « latest »
        self.assertRegex(ic.ROOFER_IMAGE, r':v\d+\.\d+\.\d+$')


class Network(unittest.TestCase):
    class Opener(object):
        def __init__(self, outcomes):
            self.outcomes, self.calls = list(outcomes), 0

        def open(self, request, timeout=None):
            self.calls += 1
            o = self.outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o

    @staticmethod
    def http_error(code, retry_after=None):
        h = email.message.Message()
        if retry_after:
            h['Retry-After'] = retry_after
        return urllib.error.HTTPError('https://x', code, 'x', h, None)

    def test_retries_on_429_then_succeeds(self):
        op, waits = self.Opener([self.http_error(429), self.http_error(503, '3'), io.BytesIO(b'ok')]), []
        r = ic.open_url('https://x/y', opener=op, sleep=waits.append)
        self.assertEqual(r.read(), b'ok')
        self.assertEqual(op.calls, 3)
        self.assertEqual(waits, [1, 3.0])   # délai croissant, puis celui que demande le serveur

    def test_gives_up_after_retries(self):
        op = self.Opener([self.http_error(429)] * 3)
        with self.assertRaises(ic.Error):
            ic.open_url('https://x/y', opener=op, sleep=lambda s: None, retries=2)
        self.assertEqual(op.calls, 3)

    def test_no_retry_on_404_and_400_only_when_asked(self):
        op = self.Opener([self.http_error(404)])
        with self.assertRaises(ic.Error):
            ic.open_url('https://x/y', opener=op, sleep=lambda s: None)
        self.assertEqual(op.calls, 1)
        op = self.Opener([self.http_error(400), io.BytesIO(b'ok')])
        self.assertEqual(ic.open_url('https://x/y', opener=op, sleep=lambda s: None, retry_status=ic.RETRY_STATUS + (400,)).read(), b'ok')

    def test_json_response_that_is_not_json_is_retried(self):
        op, waits = self.Opener([io.BytesIO(b''), io.BytesIO(b'<html>erreur</html>'), io.BytesIO(b'{"a": 1}')]), []
        self.assertEqual(ic.fetch_json('https://x/y', opener=op, sleep=waits.append), {'a': 1})
        self.assertEqual(waits, [1, 2])
        op = self.Opener([io.BytesIO(b'<html>erreur</html>') for _ in range(4)])
        with self.assertRaises(ic.Error) as cm:
            ic.fetch_json('https://x/y', opener=op, sleep=lambda s: None)
        self.assertIn('pas du JSON', str(cm.exception))

    def test_https_only(self):
        with self.assertRaises(ic.Error):
            ic.open_url('http://data.geopf.fr/x')
        with self.assertRaises(ic.Error):
            ic.open_url('file:///etc/passwd')

    def test_lookup_tile(self):
        feature = {'features': [{'properties': {'coordonnees_nw': TILE, 'url_npl': SOURCE['url'], 'date_edition': '2025-02-20T00:00:00Z'}}]}
        with mock.patch.object(ic, 'fetch_json', return_value=feature) as f:
            self.assertEqual(ic.lookup_tile(TILE), SOURCE)
        url = f.call_args[0][0]
        self.assertIn('BBOX=846500,6493500,846500,6493500,EPSG:2154', url)
        self.assertIn('IGNF_LIDAR-HD_METADONNEE:metadata', url)
        for bad in ({'features': []}, {'features': [{'properties': {'coordonnees_nw': '0847-6494', 'url_npl': 'u'}}]},
                    {'features': [{'properties': {'coordonnees_nw': TILE}}]}):
            with mock.patch.object(ic, 'fetch_json', return_value=bad):
                with self.assertRaises(ic.Error):
                    ic.lookup_tile(TILE)

    def test_truncated_bdtopo_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ic, 'fetch_json', return_value={'features': [{}] * 3, 'numberMatched': 5}):
                with self.assertRaises(ic.Error):
                    ic.fetch_bdtopo(TILE, os.path.join(tmp, 'b.geojson'), lambda m: None)
            self.assertFalse(os.path.exists(os.path.join(tmp, 'b.geojson')))
            with mock.patch.object(ic, 'fetch_json', return_value={'features': [{}] * 3, 'numberMatched': 3}):
                ic.fetch_bdtopo(TILE, os.path.join(tmp, 'b.geojson'), lambda m: None)
            self.assertTrue(os.path.exists(os.path.join(tmp, 'b.geojson')))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache, self.out = os.path.join(self.tmp, 'cache'), os.path.join(self.tmp, 'site')
        self.docker = FakeDocker()
        self.logs = []

    def args(self, **kw):
        return Args(tiles=[TILE], cache=self.cache, output=self.out, **kw)

    def download(self, **kw):
        def fake_download(url, dest, log, **k):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _Once(dest, 'wb').write(b'copc')

        def fake_bdtopo(tile, dest, log, **k):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _Once(dest, 'w').write('{}')
        with mock.patch.object(ic, 'lookup_tile', return_value=SOURCE), mock.patch.object(ic, 'download_file', fake_download), \
                mock.patch.object(ic, 'fetch_bdtopo', fake_bdtopo):
            return ic.cmd_download(self.args(**kw), run=self.docker, log=self.logs.append)

    def test_download_writes_files_and_index(self):
        self.assertEqual(self.download(), ic.EXIT_OK)
        bld, veg = os.path.join(self.out, 'buildings', TILE + '.bld'), os.path.join(self.out, 'vegetation', TILE + '.veg')
        self.assertEqual(_Once(bld, 'rb').read()[:4], b'BLD1')
        self.assertEqual(_Once(veg, 'rb').read()[:4], b'VEG1')
        # le .bld issu de la fausse sortie de roofer est celui de la fixture : le contrat tient de bout en bout
        self.assertEqual(_Once(bld, 'rb').read(), _Once(os.path.join(FIX, 'fake.bld'), 'rb').read())
        index = json.load(_Once(os.path.join(self.out, 'index.json'), encoding='utf-8'))
        self.assertEqual(index['format'], ic.FORMAT_VERSION)
        self.assertEqual(index['licence'], ic.LICENCE)
        entry = index['dalles'][TILE]
        self.assertEqual(entry['integrateur'], ic.__version__)
        self.assertEqual(entry['outils'], ic.TOOLS)
        self.assertEqual(entry['source'], SOURCE)
        for key, path in (('batiments', bld), ('vegetation', veg)):
            blob = _Once(path, 'rb').read()
            self.assertEqual(entry[key]['taille'], len(blob))
            self.assertEqual(entry[key]['sha256'], hashlib.sha256(blob).hexdigest())
        self.assertEqual(entry['batiments']['nombre'], 3)
        self.assertEqual(sum(1 for c in self.docker.commands if ic.ROOFER_IMAGE in c), 1)
        self.assertEqual(sum(1 for c in self.docker.commands if 'pipeline' in c), 2)   # deux passes PDAL

    def test_cache_is_reused(self):
        self.download()
        n = len(self.docker.commands)
        shutil.rmtree(self.out)
        self.download()   # mêmes fichiers en cache : aucun nouvel appel à Docker
        self.assertEqual(len(self.docker.commands), n)

    def test_up_to_date_tile_is_skipped_with_code_10(self):
        self.download()
        prev = os.path.join(self.out, 'index.json')
        out2 = os.path.join(self.tmp, 'site2')
        self.out = out2
        self.assertEqual(self.download(previous_index=prev), ic.EXIT_UP_TO_DATE)
        self.assertFalse(os.path.exists(out2))   # rien n'est écrit
        self.assertEqual(self.download(previous_index=prev, force=True), ic.EXIT_OK)

    def test_new_edition_or_new_version_regenerates(self):
        self.download()
        prev = json.load(_Once(os.path.join(self.out, 'index.json'), encoding='utf-8'))
        f = ('buildings', 'vegetation')
        self.assertTrue(ic.is_current(prev, TILE, SOURCE, f))
        self.assertFalse(ic.is_current(prev, TILE, dict(SOURCE, edition='2026-01-01'), f))
        self.assertFalse(ic.is_current(prev, TILE, dict(SOURCE, edition=None), f))
        self.assertFalse(ic.is_current(prev, '0847-6494', SOURCE, f))
        with mock.patch.object(ic, '__version__', '9.9.9'):
            self.assertFalse(ic.is_current(prev, TILE, SOURCE, f))
        with mock.patch.object(ic, 'TOOLS', {'roofer': 'autre', 'pdal': ic.PDAL_IMAGE}):
            self.assertFalse(ic.is_current(prev, TILE, SOURCE, f))
        del prev['dalles'][TILE]['vegetation']
        self.assertFalse(ic.is_current(prev, TILE, SOURCE, f))   # une fonction manque : à produire
        self.assertTrue(ic.is_current(prev, TILE, SOURCE, ('buildings',)))

    def test_features_restrict_output(self):
        self.download(features=['vegetation'])
        self.assertFalse(os.path.exists(os.path.join(self.out, 'buildings')))
        self.assertTrue(os.path.exists(os.path.join(self.out, 'vegetation', TILE + '.veg')))
        self.assertFalse(any(ic.ROOFER_IMAGE in c for c in self.docker.commands))

    def download_with_bdtopo(self, content, roofer_writes):
        """Comme download(), avec un GeoJSON BD TOPO donné ; roofer n'écrit rien si roofer_writes est faux."""
        def fake_bdtopo(tile, dest, log, **k):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _Once(dest, 'w').write(content)

        def fake_download(url, dest, log, **k):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _Once(dest, 'wb').write(b'copc')
        docker = self.docker

        def run(cmd):
            if ic.ROOFER_IMAGE in cmd and not roofer_writes:
                docker.commands.append(cmd)
                return
            docker(cmd)
        with mock.patch.object(ic, 'lookup_tile', return_value=SOURCE), mock.patch.object(ic, 'download_file', fake_download), \
                mock.patch.object(ic, 'fetch_bdtopo', fake_bdtopo):
            return ic.cmd_download(self.args(), run=run, log=self.logs.append)

    def roofer_calls(self):
        return sum(1 for c in self.docker.commands if ic.ROOFER_IMAGE in c)

    def assert_empty_bld(self):
        bld = _Once(os.path.join(self.out, 'buildings', TILE + '.bld'), 'rb').read()
        self.assertEqual(bld[:4], b'BLD1')
        self.assertEqual(len(bld), 40)   # en-tête seul : 0 sommet, 0 triangle, 0 emprise
        index = json.load(_Once(os.path.join(self.out, 'index.json'), encoding='utf-8'))
        self.assertEqual(index['dalles'][TILE]['batiments']['nombre'], 0)
        self.assertEqual(_Once(os.path.join(self.out, 'vegetation', TILE + '.veg'), 'rb').read()[:4], b'VEG1')

    def test_tile_without_bdtopo_building_gives_empty_bld_without_roofer(self):
        self.assertEqual(self.download_with_bdtopo('{"features": []}', roofer_writes=True), ic.EXIT_OK)
        self.assertEqual(self.roofer_calls(), 0)
        self.assert_empty_bld()

    def test_few_footprints_all_rebuilt_elsewhere_give_empty_bld(self):
        # bâtiment à cheval sur la limite : roofer ne le reconstruit pas dans cette dalle
        self.assertEqual(self.download_with_bdtopo('{"features": [{}]}', roofer_writes=False), ic.EXIT_OK)
        self.assertEqual(self.roofer_calls(), 1)
        self.assert_empty_bld()

    def test_many_footprints_without_roofer_output_is_an_error(self):
        with self.assertRaises(ic.Error):
            self.download_with_bdtopo(json.dumps({'features': [{}] * (ic.MAX_UNBUILT + 1)}), roofer_writes=False)

    def test_unknown_footprint_count_without_roofer_output_is_an_error(self):
        with self.assertRaises(ic.Error):
            self.download_with_bdtopo('{}', roofer_writes=False)

    def test_purge_removes_work_dir(self):
        self.download(purge=True)
        self.assertFalse(os.path.exists(os.path.join(self.cache, TILE)))
        self.assertTrue(os.path.exists(os.path.join(self.out, 'index.json')))

    def test_convert_offline_needs_cache(self):
        with self.assertRaises(ic.Error):
            ic.cmd_convert(self.args(), run=self.docker, log=self.logs.append)
        self.download()
        shutil.rmtree(self.out)
        self.assertEqual(ic.cmd_convert(self.args(), run=self.docker, log=self.logs.append), ic.EXIT_OK)
        self.assertIsNone(json.load(_Once(os.path.join(self.out, 'index.json')))['dalles'][TILE]['source'])

    def test_strict_fails_on_warnings(self):
        w = ['x']
        self.assertEqual(ic._finish(w, False, lambda m: None, 'ok'), ic.EXIT_OK)
        self.assertEqual(ic._finish(w, True, lambda m: None, 'ok'), ic.EXIT_ERROR)
        self.assertEqual(ic._finish([], True, lambda m: None, 'ok'), ic.EXIT_OK)

    def test_main_reports_errors_with_code_1(self):
        self.assertEqual(ic.main(['download', '--tiles', 'zz', '--output', self.out]), ic.EXIT_ERROR)
        self.assertEqual(ic.main(['download', '--output', self.out]), ic.EXIT_ERROR)


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def site(self, name, tiles):
        d = os.path.join(self.tmp, name)
        dalles = {}
        for t, content in tiles.items():
            for sub, key, ext in (('buildings', 'batiments', 'bld'), ('vegetation', 'vegetation', 'veg')):
                blob = (t + sub + content).encode()
                p = os.path.join(d, sub, '%s.%s' % (t, ext))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                _Once(p, 'wb').write(blob)
                dalles.setdefault(t, {'integrateur': '0.1.0', 'outils': ic.TOOLS, 'source': None})[key] = {
                    'fichier': '%s/%s.%s' % (sub, t, ext), 'taille': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        ic.write_index(d, ic.new_index(dalles))
        return d

    def merge(self, partials, previous=None):
        out = os.path.join(self.tmp, 'out')
        shutil.rmtree(out, True)
        args = mock.Mock(output=out, partials=partials, previous_index=previous)
        ic.cmd_merge(args, log=lambda m: None)
        return out, json.load(_Once(os.path.join(out, 'index.json'), encoding='utf-8'))

    def test_previous_tiles_are_kept_and_new_ones_replace(self):
        old = self.site('old', {'0846-6494': 'v1', '0847-6494': 'v1'})
        new = self.site('new', {'0846-6494': 'v2', '0845-6494': 'v2'})
        out, index = self.merge([new], previous=os.path.join(old, 'index.json'))
        self.assertEqual(sorted(index['dalles']), ['0845-6494', '0846-6494', '0847-6494'])
        self.assertEqual(_Once(os.path.join(out, 'buildings', '0847-6494.bld'), 'rb').read(), b'0847-6494buildingsv1')   # reprise
        self.assertEqual(_Once(os.path.join(out, 'buildings', '0846-6494.bld'), 'rb').read(), b'0846-6494buildingsv2')   # remplacée
        self.assertEqual(len(os.listdir(os.path.join(out, 'vegetation'))), 3)

    def test_without_previous_index(self):
        _, index = self.merge([self.site('a', {'0846-6494': 'x'}), self.site('b', {'0847-6494': 'x'})])
        self.assertEqual(sorted(index['dalles']), ['0846-6494', '0847-6494'])
        _, index = self.merge([self.site('c', {'0846-6494': 'x'})], previous=os.path.join(self.tmp, 'absent.json'))   # première exécution
        self.assertEqual(list(index['dalles']), ['0846-6494'])

    def test_corrupted_file_is_refused(self):
        old = self.site('old', {'0846-6494': 'v1'})
        _Once(os.path.join(old, 'buildings', '0846-6494.bld'), 'ab').write(b'!')
        with self.assertRaises(ic.Error):
            self.merge([], previous=os.path.join(old, 'index.json'))

    def test_other_format_and_dirty_output_are_refused(self):
        old = self.site('old', {'0846-6494': 'v1'})
        idx = json.load(_Once(os.path.join(old, 'index.json')))
        idx['format'] = 99
        with open(os.path.join(old, 'index.json'), 'w') as fh:
            json.dump(idx, fh)
        with self.assertRaises(ic.Error):
            self.merge([], previous=os.path.join(old, 'index.json'))
        out = os.path.join(self.tmp, 'out')
        os.makedirs(out, exist_ok=True)
        _Once(os.path.join(out, 'x'), 'w').write('x')
        with self.assertRaises(ic.Error):
            ic.cmd_merge(mock.Mock(output=out, partials=[], previous_index=None), log=lambda m: None)

    def test_empty_merge_is_refused(self):
        with self.assertRaises(ic.Error):
            ic.cmd_merge(mock.Mock(output=os.path.join(self.tmp, 'o'), partials=[], previous_index=None), log=lambda m: None)


class Version(unittest.TestCase):
    def test_version_and_changelog(self):
        self.assertRegex(ic.__version__, r'^\d+\.\d+\.\d+$')
        changelog = _Once(os.path.join(HERE, 'CHANGELOG.md'), encoding='utf-8').read()
        self.assertIn('## [Non publié]', changelog)
        versions = re.findall(r'^## \[(\d+\.\d+\.\d+)\]', changelog, re.M)
        if versions:   # première version publiée : le journal et le code doivent s'accorder
            self.assertEqual(versions[0], ic.__version__)


if __name__ == '__main__':
    unittest.main()
