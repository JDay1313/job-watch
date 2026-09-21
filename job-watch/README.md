# Job Watch

Checks the career sites of companies you choose every 30 minutes. When a new job is posted, it adds the title, a short description, and a link to the posting to your own job feed website, and can also ping you on Discord, Slack, or email. It runs free on GitHub; no server or computer left on.

## Setup (about 15 minutes, no coding)

1. **Create a free GitHub account** at github.com if you don't have one.
2. **Create a new repository**: click **+** (top right) then **New repository**. Name it `job-watch`, choose **Public** (needed for the free website on a free account; your job list will be visible to anyone with the link), and click **Create repository**.
3. **Upload the files**: on the new repo page click **uploading an existing file**, then drag in everything from this folder, including the `.github` folder. (If your computer hides folders starting with a dot, press Cmd+Shift+. on Mac, or turn on "Hidden items" in Windows Explorer's View menu.) Click **Commit changes**.
4. **Allow the robot to save results**: Settings → Actions → General → under *Workflow permissions* choose **Read and write permissions** → Save.
5. **Turn on the website**: Settings → Pages → *Source*: **Deploy from a branch**, Branch: **main**, folder **/docs** → Save. After a minute your feed is at `https://YOUR-USERNAME.github.io/job-watch/`.
6. **Choose your companies**: open `companies.yaml`, click the pencil icon, edit the list, and commit. Instructions for each company type are at the top of that file.
7. **Run the first check**: Actions tab → *Check for new jobs* → **Run workflow**. After that it runs by itself every 30 minutes.

The first check of each company records every job that's already open without alerting you, so from then on you only hear about genuinely new postings. To see current openings too, add `SHOW_EXISTING: "1"` under `env:` in `.github/workflows/monitor.yml` for one run.

## Finding a company's job board type

Open any single job posting on the company's careers page and look at the address bar (or the address of the "Apply" button):

| You see | In companies.yaml |
|---|---|
| `greenhouse.io/stripe` | `type: greenhouse`, `id: stripe` |
| `jobs.lever.co/netflix` | `type: lever`, `id: netflix` |
| `jobs.ashbyhq.com/openai` | `type: ashby`, `id: openai` |
| `jobs.smartrecruiters.com/Visa` | `type: smartrecruiters`, `id: Visa` |
| `something.wd1.myworkdayjobs.com/...` | `type: workday`, `url:` the careers page address |
| `recruiting.ultipro.com/...` or `....rec.pro.ukg.net/...` | `type: ukg`, `url:` the job board address |
| `jobs.dayforcehcm.com/...` | `type: dayforce`, `url:` |
| `workforcenow.adp.com/...` | `type: adp`, `url:` |
| `something.bamboohr.com/careers` | `type: bamboohr`, `url:` |
| `careers.hireology.com/...` | `type: hireology`, `url:` |
| `something.isolvedhire.com/jobs/` | `type: isolved`, `url:` |
| `careers-something.icims.com/...` | `type: icims`, `url:` |
| `paycomonline.net/...` | `type: paycom`, `url:` |
| `recruitingbypaycor.com/...` | `type: paycor`, `url:` |
| `teamworkonline.com/...` (any list of jobs) | `type: teamwork`, `url:` |
| anything else | `type: page`, `url:` the careers page, optionally `link_contains:` a piece of text found in every job link |

For `type: page`, Job Watch looks for links that look like individual job postings. If it can't find any, it switches to watching the page for changes and sends a "Careers page updated" alert with a link whenever the page changes. Sites that ask automated tools to stay out (in their robots.txt) are left alone and marked on your feed page, unless you add `ignore_robots: true` to that entry. Add `every_minutes: 120` to any entry to check it less often.

You can leave out `name:` and Job Watch will use the organization name the job board shows. Check the names after the first run and add a `name:` line to any you'd like to change.

If a company shows "check failed" at the bottom of your feed page, open the Actions tab and click the latest run to see the details.

## Getting alerts (optional)

Add any of these under Settings → Secrets and variables → Actions → **New repository secret**:

- **Discord** (easiest, great phone notifications): in a Discord server you own, Server Settings → Integrations → Webhooks → New Webhook → Copy URL. Save it as `DISCORD_WEBHOOK_URL`.
- **Slack**: create an Incoming Webhook and save it as `SLACK_WEBHOOK_URL`.
- **Email** (one summary email per check that finds jobs): set `EMAIL_TO` (where to send), `SMTP_USER` (your Gmail address), and `SMTP_PASSWORD` (a Gmail *app password*, created at myaccount.google.com/apppasswords; your normal password won't work). For non-Gmail providers also set `SMTP_HOST` and `SMTP_PORT`.

## Narrowing results

With this many sites (the TeamWork Online "all sports" feed alone posts around 100 jobs a day), filters make the feed much more useful.

In `companies.yaml`, use `filters` to only keep titles containing certain words (`keywords`), skip titles with certain words (`exclude`), or require a location (`locations`). You can also put `keywords`, `exclude`, or `locations` on an individual company.

## Good to know

- GitHub's scheduled runs sometimes start a few minutes late, and pause after 60 days without any repository activity (you'll get an email; one click re-enables them).
- To check more or less often, change the `cron` line in the workflow file. `*/15 * * * *` is every 15 minutes; `0 * * * *` is hourly.
- Please keep the list to sites you'd visit yourself and don't set the schedule more often than every 15 minutes.
- Run it on your own computer instead with `pip install -r requirements.txt` then `python monitor.py`, and open `docs/index.html`.
