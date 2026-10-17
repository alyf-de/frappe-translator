# Scheduled runs with automatic pull requests

This guide shows how to run frappe-translator every night on a small Linux server for several branches (for example `version-15-hotfix`, `version-16-hotfix` and `develop`) and open a pull request with the new translations.

The guide was written for a Debian 13 server with 2 CPUs, 4 GB RAM and 40 GB disk. It has not been tested end to end yet.

## Why a server and not GitHub Actions

frappe-translator stores its term extraction cache and resume state in the bench folder (`glossary_extracted.json`, `.frappe-translator-progress.json`). The glossary files are in `glossary_dir` (default `<bench>/glossaries`). Keep them in a git-versioned folder that all benches share, and set `glossary_dir` in the config. On a server these files stay between runs, so a run that stops at the token limit continues the next night. A GitHub-hosted runner has a 6 hour job limit and starts empty, so you must save and restore these files yourself.

## What the tool needs

frappe-translator does not need a running bench. It only reads `apps/<app>/<app>/locale/main.pot` and the `.po` files next to it. A "bench" for this guide is a folder with an `apps/` subfolder that contains git checkouts.

## 1. Install the tools

```bash
sudo apt install git gh
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code, no Node.js needed
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install https://github.com/alyf-de/frappe-translator.git
```

If `gh` is not in the Debian repositories, use the [GitHub CLI apt repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).

Set up GitHub access and a git identity for the commits:

```bash
gh auth login
gh auth setup-git
git config --global user.name "Translation Bot"
git config --global user.email "bot@example.com"
```

## 2. Create a Claude token

Cron cannot log in interactively. Create a long-lived token:

```bash
claude setup-token
```

The token is valid for about one year. Set a reminder to replace it. The token uses the same usage limits as your interactive Claude Code sessions.

Store it in a file that only your user can read:

```bash
echo 'export CLAUDE_CODE_OAUTH_TOKEN="<token>"' > ~/.translate.env
chmod 600 ~/.translate.env
```

## 3. Add swap

Each Claude call is a separate Node.js process. With 4 GB RAM, swap prevents the out-of-memory killer from stopping other services on the server:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Clone the apps

Use one clone per app and one git worktree per branch. All branches share one object store, which saves disk space. `--filter=blob:none` downloads old file versions only when git needs them.

```bash
mkdir -p ~/translate/repos
for APP in frappe erpnext hrms; do
  git clone --filter=blob:none --no-checkout --origin upstream "https://github.com/frappe/$APP" ~/translate/repos/$APP
  cd ~/translate/repos/$APP
  git checkout --detach -q upstream/HEAD
  git remote add origin "https://github.com/<your-fork-org>/$APP"
  gh repo set-default "frappe/$APP"
  for BASE in version-15-hotfix version-16-hotfix develop; do
    mkdir -p ~/translate/$BASE/apps
    git worktree add ~/translate/$BASE/apps/$APP "upstream/$BASE" -B "$BASE"
  done
done
```

The result:

```
~/translate/
├── repos/<app>/                       # shared clones
├── version-15-hotfix/apps/<app>/      # worktrees, one "bench" per branch
├── version-16-hotfix/apps/<app>/
└── develop/apps/<app>/
```

If you have write access to an app repository, you can push to it directly. Then skip the `origin` fork remote and use `git remote add origin <same URL as upstream>`.

## 5. Configure

Create `~/translate/frappe-translator.toml`. Lower the concurrency to fit 4 GB RAM. The token limit is the bottleneck, not parallelism, so this costs little speed.

```toml
[general]
concurrency = 2
model = "haiku"
glossary_dir = "/home/<user>/translate/glossaries"  # git checkout, shared by all benches
```

Without `model`, the tool uses the default model of your Claude Code account. `model` sets the model for both passes, unless you also set `extract_model` for pass 1. See [docs/model-comparison.md](model-comparison.md) for the quality difference between Haiku and Sonnet.

## 6. Seed the glossaries

Do this one time, before the first scheduled run. `--seed` adds all DocType names and a short list of core Frappe terms to the glossary files. Then the command translates all terms that do not have a translation:

```bash
source ~/.translate.env
for BENCH in ~/translate/*/; do
  [ -d "$BENCH/apps" ] || continue
  frappe-translator glossary "$BENCH" --model opus --extract-model haiku --seed --config ~/translate/frappe-translator.toml
done
cd ~/translate/glossaries && git add -A && git commit -m "chore: seed glossaries" && git push
```

`--extract-model haiku` uses Haiku to find the terms in all strings, and Opus only translates the terms. The loop seeds all benches, because some DocTypes exist only on one branch. Existing entries are kept, so you can run the seed again later.

## 7. The script

Save as `~/translate/translate-all.sh` and run `chmod +x` on it:

```bash
#!/usr/bin/env bash
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"   # cron has a minimal PATH
source ~/.translate.env

for BENCH in ~/translate/*/; do
  [ -d "$BENCH/apps" ] || continue      # skips repos/
  for APP_DIR in "$BENCH"apps/*/; do
    APP=$(basename "$APP_DIR")
    [ -f "$APP_DIR/$APP/locale/main.pot" ] || continue
    cd "$APP_DIR" || continue

    BASE=$(git rev-parse --abbrev-ref HEAD)
    BRANCH="translations/$BASE"
    git fetch upstream "$BASE" && git reset --hard "upstream/$BASE"   # discards local changes

    frappe-translator translate "$BENCH" --app "$APP" --mode fill-missing \
      --config ~/translate/frappe-translator.toml || continue
    git diff --quiet -- '*.po' && continue

    git switch -C "$BRANCH"
    git add -- '*.po'
    git commit -m "chore: update translations"
    git push -f origin "$BRANCH"
    gh pr create --base "$BASE" --head "$BRANCH" \
      --title "chore: update translations ($BASE)" \
      --body "Automated translations by frappe-translator." || true   # PR exists: the push updated it
    git switch "$BASE"
  done
done
```

Notes:

- The branch name is the same every night. The force push updates the open pull request instead of opening a new one.
- Benches run one after the other. They share one Claude subscription, so parallel runs only reach the token limit sooner.
- `git reset --hard` deletes local edits in the worktrees. Use these folders only for this job.
- The script does not regenerate `main.pot`. It expects upstream to keep it up to date.
- `frappe/frappe` and `frappe/erpnext` may manage translations through Crowdin. Find out if they accept direct `.po` pull requests before you include them.
- If your fork is in a different organization, `gh pr create` may need `--head <your-fork-org>:$BRANCH`.

## 8. Schedule

Run `crontab -e` and add:

```cron
0 1 * * * flock -n /tmp/translate.lock ~/translate/translate-all.sh >> ~/translate/translate.log 2>&1
```

`flock` prevents a new run from starting while the previous one is still active.

To fill the glossary with Sonnet once a month, save this script as `~/translate/update-glossary.sh` and run `chmod +x` on it. It uses one bench, because all benches share `glossary_dir`. Then it commits the changed glossary files and pushes them directly:

```bash
#!/usr/bin/env bash
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"   # cron has a minimal PATH
source ~/.translate.env

cd ~/translate/glossaries || exit 1
git pull --ff-only || exit 1   # get manual edits first, so the push does not conflict

# A run that stops at the token limit has saved its finished batches, so commit them too
frappe-translator glossary ~/translate/develop --model sonnet --config ~/translate/frappe-translator.toml
STATUS=$?

git add -A
git diff --cached --quiet || git commit -m "chore: update glossaries"
git push
exit $STATUS
```

Add a second cron line. It runs one hour before the nightly job and uses the same lock, so the nightly job skips that night if the glossary run takes longer.

```cron
0 0 1 * * flock /tmp/translate.lock ~/translate/update-glossary.sh >> ~/translate/glossary.log 2>&1
```

## First run

Run the script manually once and watch the memory use:

```bash
~/translate/translate-all.sh
ps -o rss,cmd -C claude      # in a second terminal
```
