# Putting the site on montlab (Scott's cluster)

The site will live at **https://dycast-stats.montlab.cc**. All commands are for **PowerShell on
your PC**, in the project folder, unless a step says otherwise.

What's different from PythonAnywhere:
- The code runs in a **container**: a sealed box with Python, the packages and the code inside.
  GitHub builds it every time you push, so you never install anything on the cluster.
- The box's own files are **read-only** (a cluster rule: if someone broke in, they couldn't change
  the code). Only two places can be written: `/data` (the database, on storage that survives
  restarts) and `/tmp` (scratch space, wiped on restart).
- No more alarm clock from your PC: the site checks Spotify every 30 minutes by itself, in a
  background thread, and warms the pages straight after. Visitors aren't blocked while it does,
  because the site now handles several requests at once.

---

## 1. A fresh public GitHub repo (your history stays private)

The cluster needs a **public** repo. The old repo has `stats.zip` (your listening history) in its
old commits, so it stays private as a backup and the new one starts clean.

1. Go to https://github.com/DycastCoast/spotify-stats → **Settings** → Repository name →
   `spotify-stats-old` → **Rename**. Leave it private.
2. Go to https://github.com/new → name `spotify-stats` → **Public** → don't add a README → **Create**.
3. Copy the v30 files over your project folder as usual, then in PowerShell:

```powershell
cd C:\Users\jacke\Desktop\Projects\spotify-stats\spotify-stats
Remove-Item -Recurse -Force .git          # forget the old history here (it's still in spotify-stats-old)
git init
git add .
git status
```

4. **Check the `git status` list before going on.** There must be NO `.db`, `.zip` or `.env` files
   in it. (`.gitignore` should already keep them out. If one is listed, stop and ask.)
5. Then:

```powershell
git commit -m "Spotify stats site"
git branch -M main
git remote add origin https://github.com/DycastCoast/spotify-stats.git
git push -u origin main
```

From now on, don't `git pull` on PythonAnywhere: it's only a backup.

## 2. Let GitHub build the image

1. Open the repo's **Actions** tab. "Build image" starts by itself after the push. The first build
   takes ~10–20 minutes (building for the Pis' arm64 on GitHub's normal PCs is slow). Later builds
   are faster.
2. When it has a green tick: go to https://github.com/DycastCoast?tab=packages → **spotify-stats** →
   **Package settings** → **Change visibility** → **Public**. (Only needed once.)

## 3. Tell Spotify about the new address

https://developer.spotify.com/dashboard → your app → **Settings** → **Edit** → Redirect URIs → add
`https://dycast-stats.montlab.cc/callback` → **Add** → **Save**. Keep the old ones for now.

## 4. Connect to the cluster

```powershell
tailscale switch        # pick Scott's tailnet, not your personal one
kubectl get pods -n dycast
```

You should see your `hello` pod. If it says "Unauthorized" or times out, the Tailscale switch or
login is the problem: sort that first.

## 5. The secrets (sealed, so they're safe in a public repo)

1. Check kubeseal is installed: `kubeseal --version`. If not, ask me for install steps.
2. Make a Flask secret key:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

3. Copy the template, then open the copy in your editor:

```powershell
Copy-Item k8s\secret.example.yaml k8s\secret.yaml
```

   In `k8s\secret.yaml`, replace the three values. Use the client ID and secret from your `.env`
   (or the Spotify dashboard), and the key from step 2. Save the file.
4. Seal it:

```powershell
Get-Content k8s\secret.yaml -Raw | kubeseal --scope strict --format yaml | Out-File -Encoding utf8 k8s\sealed-secret.yaml
```

   If that complains it can't fetch the certificate, look in `Desktop\Scott's server` for a
   `.pem` file and add `--cert "C:\Users\jacke\Desktop\Scott's server\<that file>.pem"` after
   `kubeseal`. (No .pem there? Ask Scott for the sealed-secrets public certificate.)
5. Apply it, then delete the unsealed one:

```powershell
kubectl apply -n dycast -f k8s\sealed-secret.yaml
Remove-Item k8s\secret.yaml
kubectl get secret spotify-stats-secrets -n dycast       # should exist after a few seconds
```

   `sealed-secret.yaml` is scrambled so only the cluster can read it: it's fine to commit and push.

## 6. Start the site

```powershell
kubectl apply -n dycast -f k8s\pvc.yaml
kubectl apply -n dycast -f k8s\deployment.yaml
kubectl apply -n dycast -f k8s\service.yaml
kubectl apply -n dycast -f k8s\ingress.yaml
kubectl get pods -n dycast -w
```

Wait until the `spotify-stats-...` pod says **Running** and **1/1** (Ctrl+C stops watching). If it
doesn't get there:
- `kubectl describe pod -n dycast -l app=spotify-stats`: the Events at the bottom say why
  (e.g. a policy refused it, or the image couldn't be pulled).
- `kubectl logs -n dycast deploy/spotify-stats`: the app's own output.

## 7. Move the database over

1. On PythonAnywhere: **Files** tab → `spotify-stats` → click the download icon next to
   **stats.db** (about 210 MB). Save it into your project folder. (It's ignored by git.)
2. Copy it into the cluster. It goes in under a temporary name, then gets swapped in, so the
   site never reads a half-copied file:

```powershell
$pod = kubectl get pods -n dycast -l app=spotify-stats -o jsonpath="{.items[0].metadata.name}"
kubectl cp .\stats.db "dycast/${pod}:/data/stats.db.new"
kubectl exec -n dycast $pod -- mv /data/stats.db.new /data/stats.db
kubectl rollout restart deployment/spotify-stats -n dycast
```

## 8. Try it

Open https://dycast-stats.montlab.cc and log in. Everyone has to log in once on the new address
anyway (the "listening now" feature needs one new Spotify permission). Then send friends the
link. The first few pages after a restart take a few seconds, until the background warm-up has
run (about a minute after it starts).

## 9. When it all works

- PythonAnywhere: **Web** tab → **Disable** (or just let it expire).
- Your PC: open **Task Scheduler** and delete the task that runs `ping_site.py` (it's not needed).
- Spotify dashboard: remove the pythonanywhere redirect URI.

---

## Updating the site later

```powershell
git add .
git commit -m "what changed"
git push
```

Wait for the green tick on the **Actions** tab, then:

```powershell
kubectl rollout restart deployment/spotify-stats -n dycast
```

## Importing someone's export on the cluster

Copy the zip in, run the import inside the pod, delete the zip:

```powershell
$pod = kubectl get pods -n dycast -l app=spotify-stats -o jsonpath="{.items[0].metadata.name}"
kubectl cp .\their_data.zip "dycast/${pod}:/tmp/their_data.zip"
kubectl exec -n dycast $pod -- python import_export.py /tmp/their_data.zip --user THEIR_ID
kubectl exec -n dycast $pod -- rm /tmp/their_data.zip
```

(Apple Music: `import_apple.py` the same way.) Pages pick up the new plays by themselves.

## Handy commands

| What | Command |
|---|---|
| Is it running? | `kubectl get pods -n dycast` |
| Its output / errors | `kubectl logs -n dycast deploy/spotify-stats --tail 50` |
| Why won't it start? | `kubectl describe pod -n dycast -l app=spotify-stats` |
| Restart it | `kubectl rollout restart deployment/spotify-stats -n dycast` |
| Back up the database | `kubectl cp "dycast/${pod}:/data/stats.db" .\stats-backup.db` (set `$pod` first, as in step 7) |
