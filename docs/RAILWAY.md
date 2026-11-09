# Deploying on Railway

## Service

| Field | Value |
|---|---|
| Source | Docker image `ghcr.io/reverseddata/bhopp-improved-notifs:latest` |
| Name | `bhopp-notifs` |
| Start command | leave empty (the image runs `bhopp-notifs run`) |
| Networking | none (no public domain, no port) |
| Replicas | 1 (two copies would send duplicate messages) |
| Restart policy | On failure |

## Volume

| Field | Value |
|---|---|
| Mount path | `/app/data` |

Without the volume, a redeploy forgets which orders were already alerted.

## Variables

| Name | Default | Required | Description |
|---|---|---|---|
| `BHOPP_TOKEN` | | yes | localStorage `bhopp.token` on bhopp.com. Secret, lasts 7 days. |
| `DISCORD_BOT_TOKEN` | | yes | Bot token from discord.com/developers. Secret. |
| `DISCORD_CHANNEL_ID` | | yes | Channel the alerts go to. |
| `DISCORD_OWNER_ID` | | yes | Your Discord user ID: who is pinged and who can use the commands. |
| `DISCORD_GUILD_ID` | | no | Your server ID, so the commands appear instantly. |
| `POLL_INTERVAL_SECONDS` | `15` | no | Seconds between checks for new sales (at least 5). |
| `DRY_RUN` | `false` | no | `true` = nothing is written to BHOPP. |
| `RAILWAY_RUN_UID` | `0` | yes (keep) | Lets the app write to the Railway volume. |

## Notes

- The image package must be **public**, or Railway can't pull it without credentials. On
  GitHub: your profile → Packages → `bhopp-improved-notifs` → Package settings → Change
  visibility → Public.
- A new image is published on every push to `main` (`latest`) and for `v*` tags.
