# CLAUDE.md

Instructions propres à ce dépôt. Elles complètent la configuration globale et, sur les points ci-dessous,
la remplacent.

## Git — jamais de push direct sur `main`

**Ne jamais pousser sur `origin/main`.** Aucune exception, y compris pour un correctif d'une ligne, une
mise à jour de documentation ou un pipeline rouge.

Tout changement suit ce chemin :

1. créer une branche depuis `main` à jour — `git switch -c <type>/<sujet>` (`feat/`, `fix/`, `docs/`,
   `chore/`) ;
2. committer sur cette branche ;
3. pousser la branche — `git push -u origin <branche>` ;
4. ouvrir une pull request — `gh pr create` ;
5. attendre que la CI soit verte, puis laisser la fusion à l'auteur du dépôt.

Ne jamais fusionner une PR sans y avoir été explicitement invité.

## Git — identité des commits

Les commits de ce dépôt portent l'identité `sarcouy <sarcouy@protonmail.com>`, jamais l'identité globale
de la machine. Vérifier `git config user.email` avant de committer ; si elle diffère, passer l'identité à
la commande :

```bash
git -c user.name=sarcouy -c user.email=sarcouy@protonmail.com commit
```

## Pull requests

- Décrire ce que la PR change **et pourquoi** ces choix ont été faits.
- Commencer par une section TL;DR, la plus courte possible.
- Ouvrir la PR en brouillon (`--draft`).
- S'assigner l'auteur du dépôt comme assignee et reviewer.
- Terminer la description par l'emoji `:factory:`.

Pas de `/spend` : c'est une quick action GitLab, sans effet sur GitHub. Le serveur MCP à utiliser ici est
**github**, et non gitlab, malgré la règle globale visant `~/workspace/**`.

## Dépôt public

Ce dépôt est public. Avant tout commit : aucun secret, aucun jeton, aucun nom de domaine interne, aucune
donnée du parc. `.gitignore` exclut `.env`, `state.db`, `packages.yaml`, `.cache/` et `.venv/` — vérifier
que rien de sensible n'a été ajouté au diff.

## Vérifications avant de committer

Les trois contrôles de la CI, dans l'environnement Poetry :

```bash
poetry run pytest -q
poetry run black --check .
poetry run pylint fdroid_headwind_mirror tests tools
```

**Lire les codes de sortie, jamais la sortie seule.** Pylint affiche « 10.00/10 » tout en sortant avec un
code non nul quand il a émis un message : la note est arrondie et ne reflète pas la présence d'un
avertissement. Un `| tail` rapporte le statut de `tail`, pas celui du linter — ne pas enchaîner de pipe
sur ces commandes. Cette erreur a déjà fait passer un pipeline rouge pour vert.

Les paquets sont ciblés explicitement dans l'appel à pylint : sans cela, il parcourt `.venv` en CI.

## Conventions du projet

- Code, identifiants et messages de commit en anglais ; documentation en français.
- Commentaires réservés aux décisions non déductibles du code, et alors auto-suffisants.
- Annotations de type strictes.
- Tests entièrement hors ligne : `httpx.MockTransport` pour Headwind comme pour F-Droid.
- Toute écriture vers Headwind reste derrière `sync --apply`, jamais active en `--dry-run`.
