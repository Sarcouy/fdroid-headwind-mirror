# fdroid-headwind-mirror

[![CI](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml)

Service de synchronisation des mises à jour d'applications F-Droid vers [Headwind MDM](https://h-mdm.com/).

La conception complète est décrite dans [docs/architecture.md](docs/architecture.md). Le détail de
l'itération en cours est dans [docs/iteration-2.md](docs/iteration-2.md).

## État

| Itération | Périmètre | État |
| --- | --- | --- |
| 1 | Client Headwind en lecture seule, état local, commande `status` | ✅ livrée |
| 2 | Client F-Droid, résolution de version, `sync --dry-run` | ✅ livrée |
| 3 | Vérifications (sha256, signataire, ABI) | ✅ livrée |
| 4 | Publication d'une version (mode URL directe) | à faire |
| 5 | Mode miroir (envoi de l'APK) | à faire |
| 6 | Rattachement aux configurations et notification | à faire |
| 7 | Ordonnancement, rapport, supervision | à faire |

Aucune écriture n'est effectuée dans Headwind avant l'itération 4.

## Installation

```bash
poetry install
```

## Configuration

### Variables d'environnement

| Variable | Obligatoire | Défaut | Rôle |
| --- | --- | --- | --- |
| `FHM_HEADWIND_URL` | oui | — | URL du panneau Headwind, avec ou sans le suffixe `/rest` |
| `FHM_HEADWIND_TOKEN` | oui | — | Jeton de l'utilisateur de service (`Authorization: Bearer`) |
| `FHM_PACKAGES_FILE` | non | `packages.yaml` | Liste déclarative des paquets suivis |
| `FHM_DATABASE_PATH` | non | `state.db` | Base SQLite d'état local |
| `FHM_CACHE_DIR` | non | `.cache` | Cache de l'index F-Droid, projeté sur les paquets suivis |
| `FHM_REQUEST_TIMEOUT` | non | `30.0` | Délai d'expiration HTTP vers Headwind, en secondes |
| `FHM_FDROID_TIMEOUT` | non | `300.0` | Délai d'expiration vers le dépôt F-Droid, en secondes |

Elles peuvent aussi être placées dans un fichier `.env` à la racine.

### Jeton Headwind

Le service n'implémente pas l'authentification par mot de passe. Créez un utilisateur de service dans
Headwind, disposant au minimum de la permission `applications`, et récupérez son `authToken` — celui-ci est
persistant. Le jeton n'est jamais journalisé.

### Fichier `packages.yaml`

Copiez [packages.yaml.example](packages.yaml.example) et adaptez-le. Le suivi est **explicitement déclaratif** :
seuls les paquets listés sont pris en compte, jamais l'intégralité du dépôt F-Droid. Un paquet retiré du
fichier est retiré du suivi à l'exécution suivante.

Le fichier peut aussi être écrit en JSON : YAML 1.2 étant un sur-ensemble de JSON, un `.json` est chargé sans
conversion. Pointez simplement `FHM_PACKAGES_FILE` dessus.

```yaml
repo:
  url: https://f-droid.org/repo
  fingerprint: 43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab

defaults:
  mirror: true
  auto_approve: false

packages:
  - pkg: org.mozilla.fennec_fdroid
    auto_approve: true
  - pkg: com.nextcloud.client
  - pkg: org.videolan.vlc
    mirror: false
```

| Clé | Portée | Rôle |
| --- | --- | --- |
| `repo.url` | globale | Dépôt F-Droid par défaut |
| `repo.fingerprint` | globale | Empreinte du dépôt, vérifiée à partir de l'itération 2 |
| `mirror` | défaut ou paquet | Héberger l'APK dans Headwind plutôt que pointer vers F-Droid |
| `auto_approve` | défaut ou paquet | Rattacher automatiquement la nouvelle version aux configurations |
| `target_abis` | défaut ou paquet | ABI à publier, par ordre de préférence — défaut `[arm64-v8a]` |
| `blocked_anti_features` | défaut ou paquet | Anti-fonctionnalités écartant une version — vide par défaut |
| `paused` | paquet | Suspendre le suivi sans retirer la déclaration |
| `repo_url` | paquet | Dépôt spécifique à ce paquet |

## Utilisation

```bash
poetry run fhm status
```

La commande confronte `packages.yaml` aux applications déclarées dans Headwind :

```
Applications Headwind: 4
Paquets suivis: 4

  org.mozilla.fennec_fdroid  OK
                             application #7, version 128.0.1
                             signataire non epingle
  com.nextcloud.client       AMBIGU
                             candidat #8 Nextcloud v3.29.0
                             candidat #9 Nextcloud (commun) (application commune) v3.28.0
  org.videolan.vlc           ABSENT DE HEADWIND
                             ajouter l'application dans Headwind avant de la suivre
```

| Statut | Signification | Action attendue |
| --- | --- | --- |
| `OK` | Une seule application Headwind porte ce package | aucune |
| `EN PAUSE` | Suivi suspendu par `paused: true` | aucune |
| `ABSENT DE HEADWIND` | Aucune application ne porte ce package | ajouter l'application dans Headwind |
| `AMBIGU` | Plusieurs applications portent ce package | retirer le doublon, ou ne pas suivre ce paquet |

Un package `AMBIGU` n'est jamais résolu automatiquement : associer la mauvaise application ferait publier
une version sur le mauvais enregistrement.

`paused: true` supprime toute alerte pour ce paquet : il n'est jamais comptabilisé comme bloquant, même s'il
est absent de Headwind ou ambigu. Le détail affiché précise alors qu'il n'est pas résolu. Mettre un paquet en
pause est donc bien une mise en sourdine complète, et non un simple report de traitement.

### Options

| Option | Rôle |
| --- | --- |
| `--json` | Sortie machine, exploitable par un outil de supervision |
| `--show-untracked` | Liste les applications Headwind absentes de `packages.yaml` |

### Codes de sortie

| Code | Signification |
| --- | --- |
| `0` | Tous les paquets suivis sont résolus |
| `1` | Au moins un paquet est `ABSENT DE HEADWIND` ou `AMBIGU` |
| `2` | Erreur de configuration, accès refusé, ou Headwind injoignable |

### Plan de mise à jour

```bash
poetry run fhm sync --dry-run
```

La commande récupère l'index F-Droid, résout la version candidate de chaque paquet suivi et la compare à
celle publiée dans Headwind. **Elle n'écrit rien dans Headwind** : la publication arrive à l'itération 4.

```
Index F-Droid: 3 paquet(s) suivi(s), timestamp 1789478586569 (source CACHE)

  org.videolan.vlc           MISE A JOUR
                             Headwind 3.6.5 (13060506) -> F-Droid 3.7.1 (13070106)
                             publication par ABI:
                               arm64-v8a    versionCode 13070106
  com.nextcloud.client       A JOUR
                             version 34.1.1 (340010190)
                             2 preversion(s) ecartee(s)
  com.pavelsof.wormhole      REFUS
                             aucune version pour arm64-v8a (disponibles: armeabi-v7a)
```

| Statut | Signification |
| --- | --- |
| `MISE A JOUR` | Une version plus récente est disponible sur F-Droid |
| `A JOUR` | Headwind porte déjà la version candidate |
| `REFUS` | Signataire divergent, ABI indisponible, ou anti-fonctionnalité bloquée |
| `ABSENT DE F-DROID` | Le paquet n'existe pas dans le dépôt |
| `IGNORE` | Paquet en pause, ou non résolu dans Headwind (voir `status`) |

La seule écriture est locale : le signataire est épinglé à la première résolution réussie, et ne peut plus
être écrasé ensuite. Une divergence ultérieure produit un `REFUS`, car Android rejette toute mise à jour
signée par une autre clé.

Le premier appel télécharge l'index complet (19 Mo en gzip) ; les suivants repartent du cache projeté, qui
ne conserve que les paquets suivis — 552 ko pour trois paquets, contre 60 Mo pour l'index entier.

### Vérification des APK

```bash
poetry run fhm sync --dry-run --verify-apk
```

Sans cette option, aucun APK n'est téléchargé : `sync --dry-run` ne lit que des métadonnées. Avec elle, les
APK des paquets à mettre à jour sont téléchargés en flux et leur empreinte sha256 comparée à celle de l'index.

```
  org.vi_server.red_screen  MISE A JOUR
                            Headwind 0.1 (0) -> F-Droid 1.2 (3)

0 a jour, 1 mise(s) a jour possible(s), 0 refus
APK verifies: 1, en echec: 0, telecharges: 17.2 ko, reutilises: 0 o
```

Les APK sont conservés sous `FHM_CACHE_DIR/apk/<paquet>/<versionCode>-<abi>.apk` et réutilisés tant que leur
empreinte reste valable — un fichier altéré est retéléchargé. Une empreinte divergente à la source refuse le
paquet et **ne laisse aucun fichier** sur disque. La taille annoncée par l'index plafonne le transfert, ce qui
évite qu'un miroir défaillant remplisse le cache.

### Chaîne de confiance et limite connue

| Maillon | Vérification |
| --- | --- |
| `entry.json` → index / diff | Empreinte sha256 comparée avant tout parsing |
| index → APK | Empreinte sha256 et taille comparées pendant le téléchargement |
| APK → signataire | Le signataire déclaré par l'index porte sur ces octets exacts, épinglé au premier suivi |
| dépôt → `entry.json` | **Non vérifié** — voir ci-dessous |

Le champ `repo.fingerprint` de `packages.yaml` n'est **pas encore contrôlé** : le service télécharge
`entry.json`, qui n'est pas signé, et non `entry.jar`. La protection actuelle repose sur HTTPS et sur la
chaîne d'empreintes ci-dessus. Valider le fingerprint demanderait de vérifier une signature JAR, ce qui fera
l'objet d'un travail dédié.

Le certificat des APK n'est volontairement pas ré-extrait : si les octets correspondent à l'empreinte de
l'index, l'affirmation de signataire de l'index porte sur ces octets. Re-dériver le certificat n'apporterait
de garantie que contre un index falsifié — lequel déclarerait de toute façon le signataire de l'APK falsifié.

## Inventaire du parc

Headwind ne collecte pas l'architecture CPU des appareils. Pour déterminer les ABI à publier, l'outil
d'inventaire agrège les modèles et versions Android enrôlés :

```bash
poetry run python tools/parc_inventory.py
```

Il utilise les mêmes variables d'environnement que la commande principale. L'architecture se déduit ensuite
du modèle, ou se lit directement sur un appareil :

```bash
adb shell getprop ro.product.cpu.abilist
```

Le parc visé ici est homogène en `arm64-v8a`.

## Développement

```bash
poetry run pytest
poetry run pylint .
poetry run black --check .
```

La suite de tests est intégralement hors ligne : les échanges HTTP sont simulés par `httpx.MockTransport`,
aucune instance Headwind n'est nécessaire.
