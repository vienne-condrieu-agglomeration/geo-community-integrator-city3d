# integrator-city3d

Intégrateur de données pour la plateforme GEO : prépare les fichiers statiques que le panneau **Maquette** du module
[geo-lidar-hd](https://github.com/vienne-condrieu-agglomeration/geo-module-lidar-hd) lit dans le navigateur, sans serveur
applicatif ni base de données :

- `buildings/<dalle>.bld` : **bâtiments LoD2**, reconstruits par [roofer](https://github.com/3DBAG/roofer) (3DBAG) à partir du
  LiDAR HD de l'IGN et des emprises de la BD TOPO ;
- `vegetation/<dalle>.veg` : **canopée** (sommet et base de la végétation en grille de 1 m), lue dans les points classés
  « végétation » du LiDAR HD avec [PDAL](https://pdal.io).

> Projet communautaire indépendant, **non affilié à Business Geografic**. GEO est un produit de Business Geografic.

Une dalle se nomme `XXXX-YYYY` : coin nord-ouest en km, Lambert-93 (la dalle 0846-6494 couvre x 846000–847000,
y 6493000–6494000, au centre de Vienne).

```
LiDAR HD (COPC, IGN) ─┬─ roofer (Docker) + BD TOPO ─ CityJSONSeq ─ buildings/cityjsonseq_to_bld.py ─ <dalle>.bld
                      └─ PDAL (Docker), 2 grilles de 1 m ─────────── vegetation/pack_veg.py ─────── <dalle>.veg
```

Python 3, bibliothèque standard uniquement ; Docker pour roofer et PDAL ; `curl`.

## Utilisation

```bash
cd src/integrators/city3d/www

./prepare-buildings.sh                       # dalle 0846-6494 : ~2 min + ~290 Mo de COPC
./prepare-buildings.sh around 0846-6494      # la dalle et ses 8 voisines
./prepare-vegetation.sh around 0846-6494     # idem pour les arbres (le COPC est partagé)
python3 -I serve.py                          # sert served/ en HTTP avec CORS (essai local)
```

Les scripts sont relançables : ce qui est déjà téléchargé ou calculé n'est pas refait. Détail, prérequis, essai dans un vrai
GEO et dépannage : [`www/README.md`](www/README.md) ; pipelines : [`buildings/README.md`](buildings/README.md) et
[`vegetation/README.md`](vegetation/README.md). Tests :

```bash
python3 -I -m unittest discover -s buildings
python3 -I -m unittest discover -s vegetation
```

## Format de sortie

C'est le contrat avec le module. **La spécification qui fait foi est celle du `GUIDE-DEV.md` du module** (sections
« Bâtiments LoD2 » et « Arbres ») ; ses lecteurs (`parseBld`, `parseVeg`) refusent un fichier dont la signature, la taille ou
l'origine sont incohérentes. Résumé, little-endian :

| Fichier | Signature | Contenu |
|---|---|---|
| `.bld` | `BLD1` | en-tête de 40 octets, sommets (x, y locaux à la dalle, z en altitude NGF), triangles, pieds de mur, emprises (trous compris) |
| `.veg` | `VEG1` | en-tête de 48 octets, puis 2 bandes de `size` × `size` uint16 (altitude maximale des classes 3 à 5, minimale de la classe 5), compressées en deflate |

Résultats attendus sur la dalle 0846-6494 (mesurés le 2026-11-13) : environ 1 471 bâtiments et 128 000 triangles, un `.bld`
d'environ 2,7 Mo (le résultat de roofer varie un peu d'une exécution à l'autre) ; 3 399 arbres, un `.veg` de 923 Ko.

Le dossier `fixtures/` contient de petits fichiers de référence (`fake.bld`, `fake.veg`, fausses sorties de roofer) : le module
les lit dans ses tests, et les tests de cet intégrateur vérifient qu'il les reproduit à l'identique. Ils fixent le contrat des
deux côtés.

## Outils lancés (non redistribués)

| Outil | Version | Licence |
|---|---|---|
| [roofer](https://github.com/3DBAG/roofer) | image Docker `3dgi/roofer:v1.0.0` | GPL-3.0 |
| [PDAL](https://pdal.io) (avec GDAL) | image `pdal/pdal@sha256:3e38c3fe…` (PDAL 2.10.2) | BSD |

Ils tournent dans des conteneurs, à la préparation des fichiers ; rien n'en est copié dans ce dépôt ni dans le module. Les
versions sont figées (le tag `latest` change ou n'existe pas).

## Licence

- **Code** : [GPL-3.0-or-later](LICENSE) — © 2026 Vienne Condrieu Agglomération.
- **Documentation** (ce README, `CHANGELOG.md`, READMEs des sous-dossiers) : [CC-BY-4.0](LICENSE-DOCS).
- **Données produites** : dérivées du LiDAR HD et de la BD TOPO de l'IGN, publiées sous
  [Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/). Mention à reprendre :
  « Source : IGN, LiDAR HD et BD TOPO®, traitement Vienne Condrieu Agglomération ».
- **Marques** : « GEO » et « Business Geografic » sont des marques de leurs titulaires ; cet intégrateur est indépendant et
  n'est ni édité ni approuvé par Business Geografic.

Les contributions se font sous la même licence, avec signature DCO : voir [CONTRIBUTING.md](CONTRIBUTING.md).

## Origine

Code repris sans réécriture de l'historique depuis `src/modules/geo-lidar-hd/scripts/city3d` du dépôt geo-api-v2, au commit
`5f11e34` (2026-11-13), puis adapté (chemins, documentation). Le dépôt de données `geo-community-data-city3d`
(GitHub Pages) n'existe pas encore.
