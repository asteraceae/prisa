# Collectors on the home server

Archives the FGC live occupancy feed every 2 minutes (~2.5 KB gzipped per
snapshot, under 2 MB a day). Needs only `python3` (3.10+) and `git`; no other packages.

```
git clone https://github.com/asteraceae/prisa.git ~/prisa
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
`rsync -a <server>:prisa/data/raw/collect/ data/raw/collect/`.
