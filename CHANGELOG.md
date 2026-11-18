# Journal des nouveautés

Toutes les évolutions notables de l'intégrateur city3d. Le format suit
[Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le versionnement
[SemVer](https://semver.org/lang/fr/).

Ce fichier s'écrit pour les **utilisateurs** de l'intégrateur, en français.

## [Non publié]

### Ajouté

- Premier jet de l'intégrateur : préparation des fichiers `<dalle>.bld` (bâtiments LoD2 reconstruits par roofer) et
  `<dalle>.veg` (canopée issue des points de végétation du LiDAR HD, lue avec PDAL) que le panneau Maquette du module
  geo-lidar-hd affiche, avec un serveur local pour les essayer. Code repris du dossier `scripts/city3d` du module
  (commit source `5f11e34`), sans réécriture
