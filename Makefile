include Makefile.dev

################################################################
# Create the virtual enviornment for testing and CI/CD

APP_ETC=app/etc

run:
	source $(API_KEY) && poetry run python -m src.robot.news_robot news_config.json

runb:
	source $(API_KEY) && poetry run python -m src.robot.news_robot news_basis.json

runs:
	source $(API_KEY) && poetry run python -m src.robot.news_robot news_ai2050.json

runk:
	source $(API_KEY) && poetry run python -m src.robot.news_robot news_keziah.json

gcloud-auth:
	source gcp-config.bash && gcloud auth application-default login --impersonate-service-account=GCP_SA_EMAIL

# Testing commands - use poetry run
test:
	poetry run pytest tests/ -v

test-ner:
	poetry run pytest tests/test_text_ner.py -v

test-news:
	poetry run pytest tests/test_news_robot.py -v


install-ubuntu:
	sudo apt-get update
	which pipx || sudo apt install -y pipx
	pipx ensurepath
	pipx install poetry --force
	which aws || sudo snap  install aws-cli --classic
	which chromium || sudo apt-get install -y chromium-browser chromium-chromedriver
	which curl || sudo apt install curl
	which node || sudo apt install -y nodejs
	which npm || sudo apt install -y npm
	which zip || sudo apt install -y zip
	which java || sudo apt install -y openjdk-21-jre-headless
	npm install
	npm ci
	make install

################################################################
