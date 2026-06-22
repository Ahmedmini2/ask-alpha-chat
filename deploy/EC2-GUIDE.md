# Ask Alpha on ONE AWS server (beginner guide)

The simplest way to run Ask Alpha on AWS: one EC2 server in UAE running all 3 processes
(API, video worker, Telegram bot) with Docker, plus Caddy for automatic HTTPS. No load
balancers, no Fargate, no IAM task roles.

## Before you start you need
1. An **AWS account** (https://aws.amazon.com → "Create an AWS Account").
2. A **domain for the API** where you can add a DNS record, e.g. `api.allegiance.ae`.
   (Required for HTTPS — your Vercel site is HTTPS and can't call a plain-HTTP API.)
3. Your **AWS access keys** (the same ones set on Railway) and all the API keys/passwords.
4. The code **pushed to GitHub** with the latest changes (ask Claude to do this).

---

## Part A — Launch the server
1. Sign in to AWS. Top-right region selector → choose **Middle East (UAE) me-central-1**.
2. Search **EC2** → open it → **Launch instance**.
3. **Name:** `askalpha`.
4. **OS image (AMI):** Ubuntu Server 24.04 LTS.
5. **Instance type:** `t3.xlarge` (4 vCPU / 16 GB). You can pick `t3.large` to save money and
   resize later.
6. **Key pair:** "Proceed without a key pair" is fine — we'll use the browser terminal.
7. **Network settings → Edit → Security group**, allow:
   - SSH, port **22**, source **My IP**
   - HTTP, port **80**, source **Anywhere (0.0.0.0/0)**
   - HTTPS, port **443**, source **Anywhere (0.0.0.0/0)**
8. **Storage:** change to **30 GB** gp3.
9. **Launch instance.** Open the instance and copy its **Public IPv4 address**.

## Part B — Point your domain at the server
In your DNS provider, add an **A record**: name `api` (→ `api.allegiance.ae`), value = the
server's Public IPv4. Save. (DNS can take a few minutes to a couple of hours.)

## Part C — Open the server terminal
EC2 console → select the instance → **Connect** → tab **EC2 Instance Connect** → **Connect**.
A black terminal opens in your browser. Everything below is pasted there.

## Part D — Install Docker + Git
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo apt-get install -y git
```

## Part E — Get the code
If the GitHub repo is private, make a token first (GitHub → Settings → Developer settings →
Personal access tokens → Tokens (classic) → Generate, scope `repo`), then:
```bash
git clone https://YOUR_GITHUB_TOKEN@github.com/Ahmedmini2/ask-alpha-chat.git
cd ask-alpha-chat/deploy
```
(If the repo is public, drop `YOUR_GITHUB_TOKEN@`.)

## Part F — Fill in your secrets
```bash
cp .env.production.example .env.production
nano .env.production
```
Replace every `REPLACE` with your real value. Save with **Ctrl+O**, Enter, then exit with
**Ctrl+X**.

## Part G — Set your API domain
```bash
nano Caddyfile
```
Change `api.example.com` to your real domain (e.g. `api.allegiance.ae`). Save & exit.

## Part H — Start everything
```bash
sudo docker compose up -d --build
```
The first build takes ~5–10 minutes (it installs the browser + ffmpeg). After it finishes:
```bash
sudo docker compose ps          # all should say "running"
sudo docker compose logs -f api # watch startup; Ctrl+C to stop watching
```

## Part I — Verify
```bash
curl https://api.allegiance.ae/health
```
You should get a small JSON OK. Also confirm the worker log shows
"HeyGen poller worker starting":
```bash
sudo docker compose logs worker | tail
```

## Part J — Point the website at the new API
In Vercel → your project → Settings → Environment Variables, set the API base URL to
`https://api.allegiance.ae` and redeploy the site. Test login + a chat + a video.

## Part K — Turn off Railway
Once the new server works, stop the Railway services. Keep them for ~a day as a fallback.

---

## Everyday operations
```bash
cd ~/ask-alpha-chat
git pull                                   # get new code
sudo docker compose -f deploy/docker-compose.yml up -d --build   # redeploy
sudo docker compose -f deploy/docker-compose.yml logs -f api     # view logs
sudo docker compose -f deploy/docker-compose.yml restart         # restart all
```

## If it's slow or runs out of memory
Stop the instance, **Actions → Instance settings → Change instance type**, pick a bigger
size, start it again. (Same disk, same setup.)
