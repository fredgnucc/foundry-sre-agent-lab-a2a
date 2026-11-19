# Runs as root on the jump VM through a managed Run Command (PUT .../runCommands/external-setup).
# labkit/external.py prepends the variables: PROM_VERSION, VENDOR_PORT, VENDOR_NAME,
# SCRAPE_INTERVAL, WRITE_URL, CLIENT_ID, COLLECTOR, VENDOR_SIM_B64. Safe to re-run.
set -euo pipefail

install -d /opt/vendor-sim /var/lib/vendor-sim /opt/prometheus /etc/prometheus
[ -f /var/lib/vendor-sim/mode ] || echo ok > /var/lib/vendor-sim/mode
echo "$VENDOR_SIM_B64" | base64 -d > /opt/vendor-sim/vendor_sim.py

cat > /etc/systemd/system/vendor-sim.service <<EOF
[Unit]
Description=Fake third-party vendor API metrics (lab)
After=network-online.target
[Service]
Environment=VENDOR_PORT=$VENDOR_PORT
ExecStart=/usr/bin/python3 /opt/vendor-sim/vendor_sim.py
DynamicUser=yes
Restart=always
[Install]
WantedBy=multi-user.target
EOF

if ! /opt/prometheus/prometheus --version 2>/dev/null | grep -q "version $PROM_VERSION "; then
  curl -fsSL "https://github.com/prometheus/prometheus/releases/download/v$PROM_VERSION/prometheus-$PROM_VERSION.linux-amd64.tar.gz" \
    | tar xz -C /opt/prometheus --strip-components=1
fi

# Agent mode: scrape and remote-write only, no local queries or TSDB. The token comes from
# IMDS for the user-assigned identity (aud https://monitor.azure.com), no key on disk.
cat > /etc/prometheus/prometheus.yml <<EOF
global:
  scrape_interval: $SCRAPE_INTERVAL
  external_labels:
    collector: $COLLECTOR
scrape_configs:
  - job_name: vendor
    static_configs:
      - targets: ["127.0.0.1:$VENDOR_PORT"]
        labels:
          vendor: $VENDOR_NAME
remote_write:
  - url: "$WRITE_URL"
    azuread:
      cloud: AzurePublic
      managed_identity:
        client_id: "$CLIENT_ID"
EOF

cat > /etc/systemd/system/prometheus-agent.service <<EOF
[Unit]
Description=Prometheus agent: scrape the vendor, remote-write to Azure Monitor (lab)
After=network-online.target vendor-sim.service
[Service]
ExecStart=/opt/prometheus/prometheus --agent --config.file=/etc/prometheus/prometheus.yml --storage.agent.path=/var/lib/prometheus-agent --web.listen-address=127.0.0.1:9090
DynamicUser=yes
StateDirectory=prometheus-agent
Restart=always
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vendor-sim prometheus-agent
systemctl restart vendor-sim prometheus-agent
sleep 20
echo "vendor-sim: $(systemctl is-active vendor-sim) | prometheus-agent: $(systemctl is-active prometheus-agent)"
curl -s "127.0.0.1:$VENDOR_PORT/metrics" | grep -v '^#'
journalctl -u prometheus-agent --since -30s --no-pager -o cat | grep -iE 'remote|error|warn' | tail -5 || true
