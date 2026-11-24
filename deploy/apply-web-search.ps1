# Run in your PowerShell terminal; sudo asks for the server password.
ssh -t user@your-server-ip 'sudo jarvis-deploy /tmp/jarvis-backend-20261019T184509Z-web-search.tar.gz dc274d8afe3f7cbd7dd758ec50fdb0d2afb15595733581137db93ae268a2a2e8'
exit $LASTEXITCODE
