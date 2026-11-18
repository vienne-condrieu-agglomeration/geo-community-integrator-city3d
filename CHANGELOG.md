# Journal des nouveautés

Toutes les évolutions notables de l'intégrateur city3d. Le format suit
[Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le versionnement
[SemVer](https://semver.org/lang/fr/).

Ce fichier s'écrit pour les **utilisateurs** de l'intégrateur, en français.

## [Non publié]

## [0.1.1] — 2026-11-18

### Corrigé

- Une dalle qui ne contient aucun bâtiment reconstruit n'échoue plus (« roofer n'a produit aucune sortie »). C'est le cas des
  dalles de campagne dont les seules emprises BD TOPO sont à cheval sur la limite d'une dalle voisine, ou sans aucune emprise :
  l'intégrateur écrit un `.bld` vide (en-tête seul, 0 bâtiment) et publie la canopée comme d'habitude. Sans emprise BD TOPO, roofer
  n'est plus lancé. À partir de 11 emprises sans aucune sortie, l'erreur est conservée : roofer a alors réellement échoué

## [0.1.0] — 2026-11-18

### Ajouté

- Premier jet de l'intégrateur : préparation des fichiers `<dalle>.bld` (bâtiments LoD2 reconstruits par roofer) et
  `<dalle>.veg` (canopée issue des points de végétation du LiDAR HD, lue avec PDAL) que le panneau Maquette du module
  geo-lidar-hd affiche, avec un serveur local pour les essayer. Code repris du dossier `scripts/city3d` du module
  (commit source `5f11e34`), sans réécriture
- Ligne de commande `integrator_city3d.py` (`download`, `convert`, `merge`) qui remplace les scripts shell `prepare-*.sh` : une
  seule implémentation, testée sans Docker. Reprise à délai croissant sur les erreurs 429 et serveur de `data.geopf.fr`, dalles
  déjà à jour ignorées d'après l'index précédent (code de sortie 10), `index.json` (version du format, tailles, sha256, versions
  des outils, édition du nuage source), rassemblement avec les dalles déjà publiées, nettoyage du cache dalle par dalle
