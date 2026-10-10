# Uses Finch if installed, otherwise Docker.
COMPOSE ?= $(shell command -v finch >/dev/null 2>&1 && echo "finch compose" || echo "docker compose")

.PHONY: up down logs data tick tick-py reset model dev dashboards

up:            ## start everything, then open http://localhost:8080
	$(COMPOSE) up --build -d
	@echo "Paani Kahan starting at http://localhost:8080  (logs: make logs)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f app

data:          ## fetch real Velachery streets + terrain (needs internet), then restart app
	python3 scripts/fetch_area.py
	-$(COMPOSE) restart app

model:         ## pull the local model for the Strands agent
	ollama pull llama3.2:3b

tick:          ## nowcast update via the SAM-built Lambda -> SNS -> SQS (LocalStack)
	cd serverless && sam local invoke PollerFunction

tick-py:       ## same Lambda handler, run directly without SAM
	APP_URL=http://localhost:8080 AWS_ENDPOINT=http://localhost:4566 python3 serverless/poller/app.py

reset:         ## back to the 4:10 PM nowcast
	curl -s -XPOST http://localhost:8080/api/internal/reset && echo

dashboards:    ## optional OpenSearch Dashboards on :5601
	$(COMPOSE) --profile dashboards up -d dashboards

dev:           ## run the app natively (OpenSearch + LocalStack still in containers)
	$(COMPOSE) up -d opensearch localstack
	pip install -r requirements.txt
	uvicorn app.server:app --port 8080 --reload

phone:          ## HTTPS phone link (Cloudflare quick tunnel); then account menu -> Open on your phone
	$(COMPOSE) --profile phone up -d tunnel
	@echo "Open the app on the laptop: account menu -> Open on your phone (scan the QR)."
