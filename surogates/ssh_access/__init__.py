"""SSH remote-access: vaulted private keys + per-agent targets.

An agent's terminal can open authenticated SSH sessions into user-configured
remote hosts.  The private key is held by an isolated ``ssh-agent`` (a separate
container in the k8s backend); the untrusted sandbox receives only the auth
socket and can authenticate but never read the key material.
"""
