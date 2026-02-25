################################################################
# Settings for deployment
#
# usage:
# AWS_REGION=local make pytest                  Local testing
# AWS_REGION=us-east-2 make sam-deploy          Deployment

export AWS_PROFILE=basistech



################################################################
## Bring in combined makefile

include Makefile.dev

install-ubuntu:
	sudo apt-get update
	which pipx || sudo apt install -y pipx
	pipx ensurepath
	pipx install poetry --force
	which aws || sudo snap install aws-cli --classic
	which chromium || sudo apt-get install -y chromium-browser chromium-chromedriver
	which curl || sudo apt install -y curl
	which node || sudo apt install -y nodejs
	which npm  || sudo apt install -y npm
	which zip  || sudo apt install -y zip
	which java || sudo apt install -y openjdk-21-jre-headless
	npm install
	npm ci
	make install

# Simulate an S3 EventBridge trigger for local testing
local-s3-event:
	@echo "Simulating S3 Object Created event for $(JOB_ID)..."
	@jq -n --arg bucket "$(LOCAL_BUCKET)" --arg key "$(JOB_ID)" \
		'{ "source": "aws.s3", "detail": { "bucket": { "name": $$bucket }, "object": { "key": $$key } } }' \
		> temp_event.json
	aws lambda invoke --function-name $(FUNCTION_NAME) --payload file://temp_event.json --endpoint-url http://localhost:3001 out.json
	@rm temp_event.json

dump-scans:
	aws dynamodb scan  --table-name cala-garage-scans  | cat

store-and-write-plates:
	aws sts get-caller-identity # make sure we are still active
	poetry run python  -m src.app.carscan --store-plates --store-file all_plates.json

################################################################

### startup
upload-google-oidc-secret:
	aws secretsmanager create-secret \
	--name google-oidc \
	--secret-string file://secrets/google_oidc_secret.json

upload-brivo-oidc-secret:
	aws secretsmanager create-secret \
	--name brivo \
	--secret-string file://secrets/brivo_combined_secrets.json


login:
	echo SSO start URL: https://basistech.awsapps.com/start/#
	echo SSO region: us-east-1
