# Upgrading

## LoseSound 0.x to 1.0

The configuration file is migrated automatically: `schemaVersion` becomes 2, `proxy` becomes
`relay` and `direct` becomes `speaker`. Unknown fields are kept, a `.bak` copy is written and a
corrupt file is preserved as `config.json.corrupt-<timestamp>`.

**Presets written with the old `direct` mode never played** because they contain a raw stream URL.
After upgrading, set those slots again with **Direct** or **Relay** (the table shows the assigned
mode).

### Steps

```bash
cd /path/to/LoseSound
git fetch origin
git reset --hard origin/main

# optional: backup the volume before the first start with the new image
docker run --rm -v losesound_preset_proxy_data:/data:ro -v "$PWD/backup:/backup" alpine:3 \
  tar czf /backup/preset-proxy-$(date +%F).tgz -C /data .

# the 1.0 image runs as UID 10001; adjust an old volume once
docker run --rm -v losesound_preset_proxy_data:/data busybox chown -R 10001:10001 /data

docker compose build preset-proxy
docker compose up -d preset-proxy
python3 scripts/verify.py
```

The new compose file uses `ghcr.io/osxdoc/losesound:${LOSESOUND_VERSION:-latest}` and keeps a
`build:` section. A `docker compose pull` switches to the published image.

---

## AfterTouch upgrade (0.77 → 0.138.x)

AfterTouch versions before 0.99 do not implement the Orion endpoint and do not serve `/marge`,
even though the registry advertises them. Upgrading fixes both, adds the relative station locations
and the Health checks. It is a separate maintenance step with a rollback path.

### 1. Back up

```bash
mkdir -p /home/dock/backups
docker run --rm -v losesound_aftertouch_data:/data:ro -v /home/dock/backups:/backup alpine:3 \
  tar czf /backup/aftertouch-$(date +%F).tgz -C /data .
docker run --rm -v losesound_preset_proxy_data:/data:ro -v /home/dock/backups:/backup alpine:3 \
  tar czf /backup/preset-proxy-$(date +%F).tgz -C /data .
curl -s http://SPEAKER_IP:8090/presets > /home/dock/backups/presets-$(date +%F).xml
curl -s http://SPEAKER_IP:8090/info > /home/dock/backups/info-$(date +%F).xml
cp /home/dock/LoseSound/.env /home/dock/backups/env-$(date +%F)
docker image inspect ghcr.io/gesellix/bose-soundtouch:latest --format '{{json .RepoDigests}}' \
  > /home/dock/backups/aftertouch-image-digest.txt
```

Note the old image digest - it is the fastest rollback.

### 2. Update the image

Set `SOUNDTOUCH_VERSION` in `.env` (the compose file pins it, default `0.138.1` - the GHCR tag has
no `v` prefix):

```bash
docker compose pull aftertouch
docker compose up -d aftertouch
docker compose logs --tail 50 aftertouch
```

Verify:

```bash
curl -s http://HOST:8091/health
curl -s http://HOST:8091/setup/version
curl -s http://HOST:8091/bmx/registry/v1/services | python3 -m json.tool
```

All registry base URLs should answer, and the Orion station probe should return JSON:

```bash
python3 scripts/verify.py
```

### 3. Migrate the speaker again

The old marge URL contained `/marge`. Migrate with the canonical base URL:

```bash
curl -X POST "http://HOST:8091/setup/migrate/SPEAKER_IP?method=xml&target_url=http://HOST:8091&marge_url=http://HOST:8091"
```

Fallback methods: `method=telnet` (port 17000), `method=resolv` (DNS interception, requires HTTPS
and CA trust). The speaker's SSH port (22) must be open for `xml`.

Check the result:

```bash
curl -s http://SPEAKER_IP:8090/info | grep -o '<margeURL>[^<]*'
```

The URL must not end with `/marge`. If the speaker loses radio sources after this step, reboot it or
send `sourcesUpdated` (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

### 4. Re-check presets

Open the LoseSound UI:

1. The banner should no longer show `marge_unreachable`/`orion_unavailable`.
2. Use **Check stream** and set each slot again in the mode you want (`orion` is now available).
3. Use **Play now** for every slot or press the buttons on the speaker.

Presets survive the migration, but modes and relative locations may need to be re-saved once.

### 5. Rollback

```bash
# restore the previous image (digest from step 1 or the old tag/digest)
docker compose stop aftertouch
docker run --rm -v losesound_aftertouch_data:/data -v /home/dock/backups:/backup alpine:3 \
  sh -c 'rm -rf /data/* && tar xzf /backup/aftertouch-YYYY-MM-DD.tgz -C /data'
# start the old image and migrate the speaker back to the old URL if needed
```

LoseSound keeps working during an AfterTouch rollback with the `speaker` or `relay` modes.

---

## New releases

- Container images are built from tags: `v1.0.0` → `1.0.0`, `1.0`, `latest`.
- Update a server deliberately:

```bash
docker compose pull preset-proxy
docker compose up -d preset-proxy
python3 scripts/verify.py
```

- The AfterTouch version is independent; pin it in `.env` (`SOUNDTOUCH_VERSION=0.138.1`) and upgrade
  it in the maintenance window described above.
