# Collectors on the home server

Archives the FGC live occupancy feed every 2 minutes (~2.5 KB gzipped per
snapshot, under 2 MB a day). Needs only `python3` (3.10+); no other packages.

Deployed on the team's home server on
24 Oct 2026. The server has no GitHub access, so code is copied from a laptop
checkout; run this again to update it:

```
git archive HEAD | ssh <user>@<home-server> 'mkdir -p ~/prisa && tar -x -C ~/prisa'
```

First-time setup on the server:

```
mkdir -p ~/.config/systemd/user
cp ~/prisa/deploy/systemd/prisa-collect@* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now prisa-collect@fgc.timer
loginctl enable-linger "$USER"   # keep the timers running when logged out
```

Check it:

```
systemctl --user list-timers 'prisa-*'
journalctl --user -u 'prisa-collect@*' --since today
ls ~/prisa/data/raw/collect/*/
```

Snapshots land in `~/prisa/data/raw/collect/<source>/<date>/`. They are
git-ignored; copy them to a laptop with
`rsync -a <user>@<home-server>:prisa/data/raw/collect/ data/raw/collect/`.
