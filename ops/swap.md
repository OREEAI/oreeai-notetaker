# VPS swap (one-shot ops step, PR 4)

The bot's resource limits (`mem_limit: 2g` per container, ceiling 3 bots =
6 g worst case) mean a full-load host is close to memory-committed. Swap is
the pressure buffer so the kernel's OOM-killer degrades gracefully — and
prefers killing a capped bot container over OreeAI's Postgres or Redis.
This is an ops runbook, not code: run it once on the production box.

Target: Debian / Ubuntu. Commands shown exactly as run; adjust only the
size (`4G` below) to your box. The bot host and the OreeAI stack share the
machine — PR 8 verifies coexistence.

## 1. Check there is no swap yet

```bash
free -h          # Swap line should read 0B
swapon --show    # empty output = no active swap
df -h /          # confirm >= 6G free before allocating 4G
```

## 2. Create the swapfile

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile        # swap is root-only; it can hold process memory
sudo mkswap /swapfile
```

`fallocate` fails on some filesystems (notably btrfs without `nocow`, and
old XFS). If it does, use `dd` instead and skip nothing else:

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=4096 status=progress
```

## 3. Enable it now

```bash
sudo swapon /swapfile
free -h          # Swap line now shows 4.0Gi
swapon --show    # /swapfile listed, type: file
```

## 4. Make it survive a reboot

Add a mount entry — use `tee -a`, never a bare `>>` (it needs root):

```bash
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Verify the fstab entry works without rebooting:

```bash
sudo swapoff /swapfile && sudo swapon --fstab && swapon --show
```

(`swapon --fstab` activates all fstab swap entries — if it errors, fix the
line before walking away.)

## 5. Tune for a database host (recommended)

Default `vm.swappiness=60` makes the kernel swap out idle Postgres buffers
under pressure. This box should lean on swap only as OOM relief:

```bash
printf '%s\n' 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-oreeai-swap.conf
sudo sysctl --system >/dev/null
cat /proc/sys/vm/swappiness   # 10
```

## Rollback

```bash
sudo swapoff /swapfile                     # takes effect immediately
sudo sed -i '\|^/swapfile[[:space:]]|d' /etc/fstab   # drop the fstab line
sudo rm /swapfile
sudo rm -f /etc/sysctl.d/99-oreeai-swap.conf
```

## Why swap is not a fix for oversubscription

The `mem_limit`/`pids_limit`/`restart: on-failure:2` envelope is the
protection (see `bot/docker-compose.yml`). Swap only buys clean failure
under momentary pressure — a 3-bot load that genuinely exceeds RAM must be
met by a bigger box or a lower `CALL_CONCURRENCY_LIMIT`, not by more swap.
Severe thrash is also an anti-pattern: Chromium recording in real time is
latency-sensitive, and a swapped bot produces silent WAVs (exit 7), not
mystery data corruption.

## Verification (manual scenario 4)

On the VPS after these steps: `free -h` shows a non-zero Swap line **and**
the `/swapfile` entry is in `/etc/fstab`. Locally (no VPS), the PR 4 test
suite asserts this document contains the required command set
(`tests/bot/test_runner.py::test_swap_doc_is_complete`).
