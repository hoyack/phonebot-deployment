# Local secret files (never commit)

Create `mysql_root_password.txt`, `freepbxuser_password.txt`, and `sasl_passwd.txt` before starting Compose, using the [installation guide](../../docs/WINDOWS-PBX.md). The SMTP file may be empty when email is disabled. Database secrets initialize **empty volumes only**; editing files does not rotate an existing database password.

Keep this directory mode 0700, files 0600; store the ARI configuration and SIP secrets in protected local storage, never an example file. Compose file secrets are mounted files, not an encrypted secret manager. Backups contain secrets and must be encrypted and access-controlled.
