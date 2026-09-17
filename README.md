# fdroid-headwind-mirror

[![CI](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml)

Service de synchronisation des mises à jour d'applications F-Droid vers [Headwind MDM](https://h-mdm.com/).

La conception complète est décrite dans [docs/architecture.md](docs/architecture.md), dont la section 12.1
détaille le déroulé de la publication. La récupération de l'index F-Droid et la règle de sélection de la
version candidate font l'objet d'une note séparée, [docs/iteration-2.md](docs/iteration-2.md).

## État

| Itération | Périmètre | État |
| --- | --- | --- |
| 1 | Client Headwind en lecture seule, état local, commande `status` | ✅ livrée |
| 2 | Client F-Droid, résolution de version, `sync --dry-run` | ✅ livrée |
| 3 | Vérifications (sha256, signataire, ABI) | ✅ livrée |
| 4 | Publication d'une version (mode URL directe) | ✅ livrée |
| 5 | ~~Mode miroir (envoi de l'APK)~~ | ❌ abandonnée |
| 6 | Rattachement aux configurations et notification | ✅ livrée |
| 7 | Ordonnancement, rapport, supervision | ✅ livrée |

L'itération 5 est abandonnée : **Headwind n'hébergera jamais les APK.** Les versions publiées pointent
vers le dépôt F-Droid et les appareils les téléchargent eux-mêmes, ce qui suppose qu'ils atteignent
`f-droid.org`. Le service ne télécharge les APK que pour en vérifier l'empreinte avant publication.

`sync --apply` est la seule commande qui écrit dans Headwind. Elle n'a jamais été exécutée contre une
instance réelle : publication et rattachement ne sont validés que face à un serveur simulé, et aucun
appareil n'a reçu de mise à jour par ce chemin.

## Installation

```bash
poetry install
```

## Configuration

### Variables d'environnement

| Variable | Obligatoire | Défaut | Rôle |
| --- | --- | --- | --- |
| `FHM_HEADWIND_URL` | oui | — | URL du panneau Headwind, avec ou sans le suffixe `/rest` |
| `FHM_HEADWIND_LOGIN` | oui | — | Identifiant de l'utilisateur de service Headwind |
| `FHM_HEADWIND_PASSWORD` | oui | — | Mot de passe de ce compte, échangé contre un JWT au démarrage |
| `FHM_PACKAGES_FILE` | non | `packages.yaml` | Liste déclarative des paquets suivis |
| `FHM_DATABASE_PATH` | non | `state.db` | Base SQLite d'état local |
| `FHM_CACHE_DIR` | non | `.cache` | Cache de l'index F-Droid, projeté sur les paquets suivis |
| `FHM_REQUEST_TIMEOUT` | non | `30.0` | Délai d'expiration HTTP vers Headwind, en secondes |
| `FHM_FDROID_TIMEOUT` | non | `300.0` | Délai d'expiration vers le dépôt F-Droid, en secondes |

Elles peuvent aussi être placées dans un fichier `.env` à la racine.

### Compte de service Headwind

Créez un utilisateur de service dans le panneau Headwind avec le rôle **User**, et non Admin. Les deux
portent `edit_application_versions`, mais « User » laisse de côté l'accès aux paramètres système —
l'interface expose des rôles, pas les permissions nommées dans le tableau de la section 5 de la conception.

Son nom est libre : le service ne présume d'aucun identifiant et utilise celui que vous déclarez.

Son identifiant et son mot de passe suffisent : au premier appel, le service les échange contre un JWT sur
`POST /rest/public/jwt/login`, puis présente ce jeton en `Authorization: Bearer` pendant toute l'exécution.
Le mot de passe n'est jamais journalisé et ne quitte le processus que sous forme d'empreinte MD5, seul
format accepté par ce point d'entrée.

L'`authToken` visible en base **n'est pas** un identifiant de connexion : les routes `/rest/private/*`
exigent une session ou un JWT, et le présenter en `Bearer` répond `HTTP 403`.

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
  auto_approve: false

packages:
  - pkg: org.mozilla.fennec_fdroid
    auto_approve: true
  - pkg: com.nextcloud.client
  - pkg: org.videolan.vlc
```

| Clé | Portée | Rôle |
| --- | --- | --- |
| `repo.url` | globale | Dépôt F-Droid par défaut |
| `repo.fingerprint` | globale | Empreinte du dépôt — déclarée mais **pas encore vérifiée**, voir plus bas |
| `auto_approve` | défaut ou paquet | Rattacher automatiquement la nouvelle version aux configurations — `false` par défaut |
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
celle publiée dans Headwind. **`--dry-run` n'écrit rien dans Headwind** — c'est le mode par défaut.

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

### Publication dans Headwind

```bash
poetry run fhm sync --apply
```

`--apply` crée dans Headwind une version pointant directement vers l'URL du dépôt F-Droid. **Les appareils
téléchargent eux-mêmes l'APK depuis `f-droid.org`** : c'est le mode de distribution retenu, et il suppose
que le parc y a un accès sortant. Headwind n'hébergera jamais les APK.

```
  org.videolan.vlc 3.7.1: created - 2 configuration(s) concernee(s), latestVersion bascule

1 version(s) creee(s), 0 ignoree(s), 0 en echec
2 configuration(s) referencent ces applications: celles marquees autoUpdate deploient la nouvelle
version sans autre action.
```

| Résultat | Signification |
| --- | --- |
| `created` | La version existe dans Headwind, `last_created_version_code` est enregistré |
| `skipped` | APK non vérifié, version déjà créée, application non résolue, ou ABI sans champ Headwind |
| `failed` | Configurations illisibles (création annulée) ou création refusée par Headwind |

Une réponse acceptée mais inexploitable compte comme `created` : Headwind a écrit, seul l'identifiant
renvoyé manque. La traiter comme un refus ferait republier la version à chaque exécution.

Quatre garde-fous encadrent l'écriture :

- **La vérification des APK est imposée** : `--apply` active `--verify-apk` d'office, et un artefact non
  vérifié n'est jamais publié. Publier une URL sans avoir constaté l'empreinte des octets servis serait une
  affirmation d'intégrité sans fondement.
- **Les configurations sont lues avant la création**, car une configuration marquée `autoUpdate` bascule dès
  l'insertion et l'état antérieur cesse alors d'être observable.
- **La création est idempotente** côté service : `last_created_version_code` empêche de republier la même
  version, Headwind ne la refusant pas de lui-même.
- **`latestVersion` est relu après la création.** S'il n'a pas basculé, Headwind n'a rien propagé — son
  classement de versions est textuel — et le rattachement explicite de l'itération 6 devient nécessaire.
  Les exécutions suivantes continuent de le signaler (`rattachement explicite requis`) au lieu de retomber
  dans un `skipped` muet.

> Cette commande n'a jamais été exécutée contre une instance Headwind réelle. Elle est validée face à un
> serveur simulé. La valeur de `autoUpdate` sur les configurations du parc reste inconnue : tant qu'elle
> n'est pas vérifiée, considérer qu'une création de version peut déclencher un déploiement immédiat. Premier
> usage recommandé : une instance de recette, un seul paquet sans conséquence.

### Rattachement aux configurations

Une fois la version créée, `--apply` la rattache aux configurations qui installaient déjà l'application,
à condition que le paquet porte `auto_approve: true`.

```
  org.videolan.vlc: linked - 2 configuration(s) rattachee(s), notification demandee

1 version(s) rattachee(s) a 2 configuration(s), 0 ignoree(s), 0 en echec
Notification demandee: les appareils ne la recevront que si le service push est configure, sinon a
leur prochaine synchronisation.
```

Le rattachement ne dépend pas du résultat du plan mais d'un seul critère d'état : une version créée et non
encore rattachée. La même règle traite donc ce qui vient d'être publié et ce qu'une exécution précédente a
laissé en suspens — un paquet dont Headwind n'avait pas adopté la version est rattrapé au passage suivant.

| Résultat | Signification |
| --- | --- |
| `linked` | Les configurations pointent sur la nouvelle version, notification demandée |
| `skipped` | `auto_approve: false`, ou aucune configuration n'installe cette application |
| `failed` | Version introuvable dans Headwind, liens illisibles, ou rattachement refusé |

Trois garanties encadrent l'écriture :

- **Les entrées sont réémises telles quelles.** L'API exige que chaque lien lui revienne intact ; le service
  les lit donc sans les typer, pour ne perdre aucun champ et ne pas convertir `versionText`, entier côté
  serveur, en chaîne.
- **`action` n'est jamais réécrit.** Le forcer à « installer » déploierait l'application sur des
  configurations qui ne la voulaient pas et annulerait une désinstallation demandée. Seul `notify` est posé,
  et uniquement là où l'application est effectivement installée.
- **La notification est demandée, pas constatée.** `notify: true` ne déclenche un push que si le service de
  notification est configuré sur l'instance ; sinon les appareils prennent la mise à jour à leur prochaine
  synchronisation. L'API ne permet pas de distinguer les deux cas.

Avec `auto_approve: false` — le défaut — la version est créée mais laissée non rattachée, et signalée à
chaque exécution. L'approbation se fait alors dans l'interface Headwind : le service ne fournit pas de
commande d'approbation.

### Rapport et supervision

```bash
poetry run fhm report
```

Restitue l'historique des exécutions et les points d'attention, sans aucun appel réseau — la commande ne lit
que la base d'état locale.

```
Derniere execution #2: OK
  debutee 2026-09-16T14:38:54+00:00, 3 paquet(s) verifie(s), 0 version(s) creee(s), 0 erreur(s)

En attente de rattachement (1):
  org.videolan.vlc: version 13070106 creee, rattachee aucune

Historique (2 derniere(s) execution(s)):
  #2  2026-09-16T14:38:54+00:00  OK       3 verifie(s), 0 creee(s), 0 erreur(s)
  #1  2026-09-16T14:38:54+00:00  WARNING  3 verifie(s), 1 creee(s), 1 erreur(s)

1 paquet(s) suivi(s)
```

Un paquet en attente de rattachement reste affiché même lorsque la dernière exécution s'est bien passée :
c'est un état durable, que seul un opérateur peut lever. Les options sont `--runs N` et `--json`, et le code
de sortie vaut `1` si la dernière exécution a produit des erreurs.

### Journal structuré

Chaque exécution de `sync` émet sur **stderr** une ligne JSON par erreur, puis une ligne de synthèse :

```json
{"run_id": 3, "packages": 3, "updates": 1, "rejections": 1, "versions_created": 0, "versions_linked": 0, "awaiting_approval": 0, "errors": 1, "event": "sync.finished", "level": "info", "timestamp": "2026-09-16T14:38:54Z"}
```

Le rapport `--json` sort sur **stdout**, le journal sur **stderr** : sous un timer, `journalctl` collecte le
second sans jamais rendre le premier inanalysable.

### Conteneur

```bash
docker build -t fdroid-headwind-mirror .
docker run --rm \
  -v fdroid_mirror_data:/data \
  -v ./packages.yaml:/config/packages.yaml:ro \
  -e FHM_HEADWIND_URL=https://mdm.example.org \
  -e FHM_HEADWIND_LOGIN=fdroid-mirror \
  -e FHM_HEADWIND_PASSWORD=... \
  -e FHM_PACKAGES_FILE=/config/packages.yaml \
  fdroid-headwind-mirror sync --apply
```

L'image tourne sous un utilisateur non privilégié et son répertoire de travail est `/data`, où les chemins
relatifs par défaut (`state.db`, `.cache`) se résolvent. **Ce volume doit être persistant** : perdre
`state.db`, c'est perdre les signataires épinglés et les deux compteurs de progression, donc republier
l'ensemble des paquets suivis à l'exécution suivante.

Sans argument, l'image exécute `sync --dry-run` : une image démarrée par mégarde n'écrit rien dans Headwind.

### Publication de l'image

Un tag de version publie l'image sur GHCR :

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Le workflow refuse de publier si le tag ne correspond pas à la version déclarée dans `pyproject.toml` : une
image mal étiquetée serait déployée sous un numéro qui ne désigne pas son contenu. Les tags produits sont
`0.1.0`, `0.1` et `latest`.

La publication est volontairement liée aux tags et non aux fusions sur `main` : le déploiement épingle une
version exacte, et un tag mouvant sur un parc qui se met à jour seul n'est pas souhaitable.

À la **première** publication, le package GHCR est privé : le rendre public dans les paramètres du dépôt,
sinon l'hôte devra s'authentifier pour le tirer.

### Exécution quotidienne

Le service n'embarque pas d'ordonnanceur. Les unités d'exemple sont dans [deploy/](deploy) :

```bash
sudo install -m 0644 deploy/fdroid-headwind-mirror.{service,timer} /etc/systemd/system/
sudo install -D -m 0640 deploy/env.example /etc/fdroid-headwind-mirror/env
sudo systemctl enable --now fdroid-headwind-mirror.timer
```

Trois points que les unités traitent explicitement :

- **Le jeton vit dans `EnvironmentFile`**, jamais dans l'unité — celle-ci est lisible par tous.
- **`WorkingDirectory` est obligatoire** : `packages.yaml`, `state.db` et le cache ont des chemins relatifs
  par défaut.
- **`RandomizedDelaySec=2h`** évite qu'un parc de services frappe `f-droid.org` à la même seconde.

L'historique s'accumule à chaque exécution. La purge est une commande explicite, jamais un effet de bord
d'une exécution planifiée :

```bash
poetry run fhm prune --days 90
```

Elle demande confirmation avant de supprimer, sauf avec `--yes`.

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
