# Contribuer à integrator-city3d

Merci de votre intérêt. Les contributions (corrections, améliorations, documentation) sont les bienvenues.

## Licence des contributions

En contribuant, vous acceptez que votre contribution soit publiée sous les licences du projet :

- le **code** sous [GPL-3.0-or-later](LICENSE) ;
- la **documentation** sous [CC-BY-4.0](LICENSE-DOCS).

## Certificat d'origine (DCO)

Chaque commit doit porter une ligne `Signed-off-by`, qui atteste que vous avez le droit de proposer cette
contribution sous la licence du projet ([Developer Certificate of Origin 1.1](https://developercertificate.org/)).
Git l'ajoute avec l'option `-s` :

```bash
git commit -s -m "fix(integrator-city3d): décrire la correction"
```

La ligne obtenue est de la forme `Signed-off-by: Prénom Nom <adresse@exemple.fr>`. Si un commit a été oublié, la
signature peut s'ajouter après coup avec `git rebase --signoff <base>`, puis un push avec `--force-with-lease`
sur **votre** branche.

## Proposer un changement

1. Ouvrez une *issue* pour une nouveauté, ou directement une demande de fusion pour une correction simple.
2. Suivez les [Conventional Commits](https://www.conventionalcommits.org/fr/v1.0.0/), en français, à l'impératif,
   avec la portée `integrator-city3d`.
3. Lancez les tests : `python3 -I -m unittest discover -s buildings`, `… -s vegetation` et
   `python3 -I -m unittest discover -s . -p 'test_integrator*.py'` (Python 3, bibliothèque standard uniquement : n'ajoutez pas de dépendance sans en discuter). Les préparations
   complètes (`integrator_city3d.py download`) demandent Docker, un accès à `data.geopf.fr` et plusieurs Go : elles ne font pas partie des tests.
4. Mettez à jour le `CHANGELOG.md` (en français, pour les utilisateurs) et, pour une version publiée, `__version__` dans
   `integrator_city3d.py` (un test vérifie qu'elle est celle du premier `## [x.y.z]` du journal). Un changement incompatible de
   `index.json` incrémente `FORMAT_VERSION`.
5. Un changement **incompatible** du format `.bld` ou `.veg` (signature, en-tête, codage) doit être décrit dans le
   `GUIDE-DEV.md` du module geo-lidar-hd, qui fait foi, et dans le README : le module lit ces fichiers.
6. Les commentaires du code sont en français, le code en anglais.

## Données

Ne commitez aucun fichier de données (nuages COPC, sorties de roofer, dossiers `work/` et `served/`, fichiers `.bld` ou
`.veg` réels) : ils sont volumineux et les tests utilisent de petits fichiers écrits à la main (dossier `fixtures/`).
Pour signaler une dalle mal reconstruite, indiquez son nom (`XXXX-YYYY`), la version de l'intégrateur et la ligne d'erreur
éventuelle ; une capture du rendu dans le module aide.
