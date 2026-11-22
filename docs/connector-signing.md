# Signing the macOS connector

macOS remembers a program's privacy grants (Full Disk Access, keychain access) by its **signing identity**. An ad-hoc
signed connector gets a new identity with every build, so after each automatic update users would have to grant Full
Disk Access again. Sign every release with the same certificate and the identity stays
`identifier "dev.agent-hub.connector" and certificate root = <certificate hash>` — grants survive updates.

## A self-signed code-signing certificate

On the machine that builds releases:

```bash
cat > cs.cnf <<'EOF'
[req]
distinguished_name=dn
prompt=no
x509_extensions=ext
[dn]
CN=Agent Hub Connector Signing
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 3650 -config cs.cnf
openssl pkcs12 -export -legacy -inkey key.pem -in cert.pem -out cs.p12 -passout pass:<choose one>
security import cs.p12 -k ~/Library/Keychains/login.keychain-db -P <the same> -T /usr/bin/codesign
```

`scripts/build.sh` then signs both macOS connectors with it (override the name with `SIGN_IDENTITY`); without the
certificate it warns and builds them unsigned.

- **Keep `key.pem` safe and out of the repository.** Losing it means a new certificate, and every user grants access once more.
- Connectors downloaded by the self-update (or `curl`) carry no quarantine flag, so users don't need to trust the certificate.
  Gatekeeper still blocks a connector downloaded with a browser; an Apple Developer ID certificate avoids that.
