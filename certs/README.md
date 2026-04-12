# mTLS setup — Laniakea (one-time, on your infra machine)

## 1. Create the internal CA
```bash
mkdir -p certs && cd certs
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -subj "/CN=laniakea-internal-ca" -out ca.crt
```

## 2. Generate the API server cert
```bash
openssl genrsa -out api.key 2048
openssl req -new -key api.key -subj "/CN=api.laniakea.internal" -out api.csr
openssl x509 -req -in api.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -out api.crt
```

## 3. Generate the agent client cert
```bash
openssl genrsa -out agent.key 2048
openssl req -new -key agent.key -subj "/CN=laniakea-agent" -out agent.csr
openssl x509 -req -in agent.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -out agent.crt
```

## 4. Distribute
- API server: api.key  api.crt  ca.crt   → API-redis-writer/certs/
- Agent:      agent.key  agent.crt  ca.crt → laniakea-agent/certs/
- ca.key stays ONLY on the infra machine — never deployed.

## 5. Start the API with mTLS
```bash
uvicorn api_queue:app \
  --ssl-keyfile  certs/api.key \
  --ssl-certfile certs/api.crt \
  --ssl-ca-certs certs/ca.crt \
  --host 0.0.0.0 --port 8443
```

## 6. Agent .env additions
```
LANIAKEA_API_URL=https://api.laniakea.internal:8443
AGENT_CERT=certs/agent.crt
AGENT_KEY=certs/agent.key
AGENT_CA_CERT=certs/ca.crt
```

