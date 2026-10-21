# MicroShift deployment

These manifests deploy the SensorTrain web application into the `sensortrain` namespace on MicroShift.

The application source is kept in `web/server.py`. Rather than committing a generated ConfigMap containing a second copy of that large file, create/update the ConfigMap directly from the source file when deploying.

## Deploy

From the repository root:

```bash
oc create namespace sensortrain --dry-run=client -o yaml | oc apply -f -

oc create configmap sensortrain-app \
  -n sensortrain \
  --from-file=server.py=web/server.py \
  --dry-run=client -o yaml | oc apply -f -

oc apply -f deploy/microshift/deployment.yaml
oc apply -f deploy/microshift/service.yaml
oc apply -f deploy/microshift/route.yaml
```

Wait for the pod:

```bash
oc rollout status deployment/sensortrain-web -n sensortrain
oc get pods,svc,route -n sensortrain
```

## Bridge configuration

The host-side bridge cannot rely on the historical ClusterIP remaining constant if the Service is ever deleted and recreated. Retrieve the current address:

```bash
oc get svc sensortrain-web -n sensortrain \
  -o jsonpath='{.spec.clusterIP}{"\n"}'
```

Use that address in `/etc/sysconfig/sensortrain-bridge` for `TELEMETRY_URL` and `CONTROL_URL`, then restart the bridge:

```bash
sudo systemctl restart sensortrain-bridge
```

See `bridge/sensortrain-bridge.env.example` for the expected variables.

## Local route

The included Route manifest retains the historical `sensortrain.test` hostname. The current train is accessed locally at `http://sensortrain.local`. These names require their own appropriate routing and name resolution; verify the environment before applying the example Route.

## Security

The deployment runs non-root, drops all Linux capabilities, prevents privilege escalation, and uses the RuntimeDefault seccomp profile. The application reads the automatically mounted Kubernetes service-account token only to query the Kubernetes API `/readyz` endpoint for the dashboard CLUSTER indicator. No credential is embedded in the repository.

## History

Telemetry history is intentionally RAM-only. Restarting or replacing the web pod clears history and desired fan state. This is expected behavior for v1.0.
