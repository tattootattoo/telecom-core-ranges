# Deployment & Setup Guide

## 1. Push the repository to GitHub

```bash
unzip telecom-core-ranges.zip
cd telecom-core-ranges

git init
git add .
git commit -m "initial commit: telecom core/backbone ranges project"

# First create an empty (public) repository on GitHub from the web UI, then:
git branch -M main
git remote add origin https://github.com/<username>/telecom-core-ranges.git
git push -u origin main
```

Important: the repository must be **Public** for the free GitHub Actions minutes to run
without a strict monthly limit (public repos get effectively unlimited Actions run time;
private repos are limited to 2000 minutes/month).

## 2. Enable write permissions for Actions

Settings → Actions → General → Workflow permissions → choose **"Read and write
permissions"**, then Save.
Without this step, the last line of the workflow (`git push`) will fail, because it needs
write access to the repository itself.

## 3. (Optional) PeeringDB API key to raise the request limit

The code works without any key. But if you run into a lot of 429 (Too Many Requests)
errors during classification:
1. Register a free account at peeringdb.com
2. Create an API Key from your account settings
3. Add it as a Secret in the repository: Settings → Secrets and variables → Actions →
   New repository secret
   Name: `PEERINGDB_API_KEY`
4. Add this line in `.github/workflows/update.yml` under the "Classify ASNs" step:
   ```yaml
   env:
     PEERINGDB_API_KEY: ${{ secrets.PEERINGDB_API_KEY }}
   ```

## 4. First run — manually first (strongly recommended)

Before relying on the automatic weekly schedule, run it manually first from the
**Actions → Update Core/Backbone Ranges → Run workflow** tab, or locally:

```bash
pip install -r requirements.txt

# Try a small region first (only 5 countries) to check the APIs are reachable
python src/fetch_asns.py --region central_asia
python src/classify_asn.py --region central_asia
python src/fetch_prefixes.py --region central_asia
python src/build_output.py

# Review output/by_region/central_asia.csv manually before moving to the other regions
```
(or the same thing in one command: `python run_pipeline.py --region central_asia`)

If the results look reasonable, expand gradually:

```bash
python src/fetch_asns.py --region all --resume
python src/classify_asn.py --region all --resume
python src/fetch_prefixes.py --region all --resume
python src/build_output.py
```
(or: `python run_pipeline.py --region all --resume`)

`--resume` is always required after the first run — without it, the code will
reprocess everything from scratch, ignoring the checkpoints.

## 5. How many runs will you need to cover every region?

Possibly just one. **A single workflow run now loops the whole fetch → classify →
fetch-prefixes → build cycle internally**, committing progress after every pass, until
either a pass produces no new data (everything is covered) or the run gets close to its
time budget (~240 minutes, under the job's 300-minute limit). Each pass still respects
the per-pass caps in `config.yml`:
- `max_countries_per_run: 30` for fetching ASNs
- `max_asns_per_run: 2500` for classification and fetching prefixes

so a run with, say, 30 countries and thousands of ASNs to get through will do several
passes on its own, back to back, without needing to be manually re-triggered between
them — check the run's log for lines like `=== iteration 2 ===` to see this happening.

A note on the 300-minute number: GitHub-hosted runners have a **hard 6-hour
(360-minute) ceiling per job** — a platform limit, not tied to public vs. private repos
or free vs. paid plans, and for GitHub-hosted runners it can only be lowered, never
raised. 300 minutes leaves a 60-minute safety buffer under that ceiling, and the loop's
own internal 240-minute budget leaves further room for whichever pass is running when
the clock runs out to finish and commit cleanly.

If a run still ends with more left to do (the log says "approaching the time budget"
rather than "no new data"), just trigger it again — manually, or let the weekly
schedule pick it up — and it resumes exactly where it left off.

## 6. Expected results

### Shape
The file `output/all_core_ranges.csv` with these columns:
```
region,country,asn,org_name,org_id,info_type,score,prefix
southeast_asia,SG,7473,Singtel,<org_id>,NSP,60,203.116.0.0/16
southeast_asia,SG,4657,StarHub,<org_id>,NSP,60,202.157.128.0/17
...
```
(`<org_id>` and the exact `score` above are illustrative — the real values depend on
each network's current PeeringDB record, which this guide can't look up offline.)
Plus a file split by region under `output/by_region/`, and `output/classification_report.csv`
covering every ASN that was evaluated (accepted or not) with its score and reason — useful
for reviewing borderline cases instead of only ever seeing the ones that made the cut.

### Approximate size
- Countries covered: 145 (across 6 regions, see `data/regions/`)
- The raw number of ASNs before classification, per country, ranges from tens (small
  countries like Pacific islands) to hundreds (large countries like Nigeria and
  Indonesia)
- After filtering to Core/Backbone only: **expect a relatively small share** of each
  country's total ASNs to be accepted (usually just tens, even in large countries) —
  because most registered ASNs are small local enterprise networks or ISPs, and that's
  exactly the point of the filter
- Each accepted ASN may announce anywhere from a single prefix to tens (or hundreds, for
  giants like major national telecom operators)

### Expected accuracy limits (important to explain to anyone using the data)
- ASNs not registered in PeeringDB are **excluded by default** (because the default
  decision when there's no data is to safely reject) — this means some small regional
  telecom operators that never registered with PeeringDB may be missing, especially in
  small island and African countries where PeeringDB registration is less common
- Some large ASNs may be tagged `Cable/DSL/ISP` in PeeringDB even though they're the sole
  national provider that also runs the country's core infrastructure (common in small
  countries) — check and adjust `config.yml → classification.name_keywords_accept` if you
  notice a company being excluded that you know is actually Core

## 7. Ongoing maintenance

No manual work is needed after the initial setup: the weekly schedule (every Sunday)
will update the cache and outputs automatically and push them as a new commit within a
reasonable time thanks to the cache (30-day TTL) — so most queries won't be repeated each
week, only for data that's expired or new.

## 8. Doing all of this from an Android phone only (no computer)

Everything above is just `git`/shell commands and a couple of settings pages on
github.com — none of it needs a desktop computer. Here's how to do it all from an Android
phone.

### Install a terminal: Termux
- Install **Termux** from **F-Droid** (f-droid.org) or the official Termux GitHub
  releases page — **not** from the Play Store. The Play Store build has been
  unmaintained/abandoned for years and can't install current packages properly.
- Open Termux and run:
  ```bash
  termux-setup-storage
  pkg update && pkg upgrade
  pkg install git unzip
  ```
  (`termux-setup-storage` lets Termux see your phone's normal storage, e.g. the
  Downloads folder — it will prompt for a permission the first time. If you also want
  to test-run the pipeline itself on the phone with `run_pipeline.py` instead of only
  via GitHub Actions, also run `pkg install python` and `pip install -r requirements.txt`
  — this is optional, since GitHub's own servers do that work once the code is pushed.)

### Get the project into Termux
Download `telecom-core-ranges.zip` to your phone (e.g. to the Downloads folder), then in
Termux:
```bash
cd ~/storage/downloads
unzip telecom-core-ranges.zip -d ~/telecom-core-ranges
cd ~/telecom-core-ranges
```

### Create a GitHub Personal Access Token (needed to push over HTTPS)
GitHub no longer accepts your account password for `git push`; you need a token instead:
1. In your phone's browser, go to github.com → your profile picture (top right) →
   **Settings** → scroll down to **Developer settings** → **Personal access tokens** →
   **Tokens (classic)** → **Generate new token (classic)**.
2. Give it a name, pick an expiration, and check the **`repo`** scope.
3. Tap **Generate token** and copy it immediately — GitHub only shows it once. Save it
   somewhere safe (e.g. your password manager); you'll paste it in as your password in
   the next step.

### Push from Termux
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global credential.helper store   # so you don't retype the token every time

git init
git add .
git commit -m "initial commit: telecom core/backbone ranges project"
git branch -M main
```
Before the last command below, create an empty **public** repository on github.com (in
your browser: the **+** icon in the top bar → **New repository** → name it, e.g.
`telecom-core-ranges` → Public → **do not** initialize with a README → Create
repository). Then:
```bash
git remote add origin https://github.com/<username>/telecom-core-ranges.git
git push -u origin main
```
When prompted, enter your GitHub **username**, and for the password, paste the **token**
you generated above.

### Finish the setup from your phone's browser
From here it's the same steps as sections 2–4 above, all doable from github.com in your
phone's browser — no more Termux needed:
- Settings → Actions → General → Workflow permissions → Read and write permissions → Save
- Actions tab → "Update Core/Backbone Ranges" → **Run workflow** to trigger the first run
  manually (GitHub's own servers do the actual work here, not your phone)
- After that, the weekly schedule takes over automatically. Check back under the Actions
  tab occasionally, especially for the first few runs, since covering all 145 countries
  will take several runs (see section 5).
