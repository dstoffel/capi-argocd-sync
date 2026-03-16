# --- VARIABLES ---
VERSION      ?= latest
REGISTRY     ?= ghcr.io
REPO         ?= dstoffel
APP_NAME     ?= capi-argocd-sync
BUNDLE_NAME  ?= $(APP_NAME)-bundle
PACKAGE_NAME ?= capi-argocd-sync.kubetbx.io

APP_IMAGE    		:= $(REGISTRY)/$(REPO)/$(APP_NAME):$(VERSION)
APP_IMAGE_LATEST    := $(REGISTRY)/$(REPO)/$(APP_NAME):latest
BUNDLE_IMAGE 		:= $(REGISTRY)/$(REPO)/$(BUNDLE_NAME):$(VERSION)
REPO_IMAGE 			?=$(REGISTRY)/$(REPO)/capi-argocd-sync-repo:latest

VENV_DIR      := src/.venv
VENV_ACTIVATE := $(VENV_DIR)/bin/activate


# --- TARGETS ---
.PHONY: test release-image release-bundle release-package all clean generate-base

$(VENV_ACTIVATE): src/requirements.txt
	@echo "==> Creating virtual environment..."
	cd src && python3 -m venv .venv
	@echo "==> Installing dependencies..."
	. $(VENV_ACTIVATE) && pip install --upgrade pip
	. $(VENV_ACTIVATE) && pip install -r src/requirements.txt pytest
	@touch $(VENV_ACTIVATE) # Met à jour la date du fichier pour Make


test: $(VENV_ACTIVATE)
	@echo "==> Running Python tests..."
	@if [ "$$CI" = "true" ] || [ "$$GITHUB_ACTIONS" = "true" ]; then \
		echo "==> ☁️ CI Environment Detected: Running ONLY mocked tests (test_sync.py)"; \
		. $(VENV_ACTIVATE) && cd src && pytest test_sync.py -v; \
	else \
		echo "==> 💻 Local Environment Detected: Running ALL tests (including E2E)"; \
		. $(VENV_ACTIVATE) && cd src && pytest -v; \
	fi

release-image: test
	@echo "==> Building Docker image $(APP_IMAGE)..."
	docker build -t $(APP_IMAGE) src/
	docker tag $(APP_IMAGE) $(APP_IMAGE_LATEST)
	@echo "==> Pushing Docker image $(APP_IMAGE)..."
	docker push $(APP_IMAGE)
	@echo "==> Pushing Docker image $(APP_IMAGE_LATEST)..."
	docker push $(APP_IMAGE_LATEST)

release-bundle: release-image
	@echo "==> Preparing Carvel bundle staging directory..."
	rm -rf .build/carvel
	mkdir -p .build/carvel/config
	cp -R deploy/carvel/config/* .build/carvel/config/

	@echo "==> Injecting image version into staging values.yml..."
	sed -i.bak -e 's|image: .*|image: "$(APP_IMAGE)"|g' .build/carvel/config/values.yml
	rm -f .build/carvel/config/values.yml.bak

	@echo "==> Resolving image digests with kbld..."
	mkdir -p .build/carvel/.imgpkg
	kbld -f .build/carvel/config/ --imgpkg-lock-output .build/carvel/.imgpkg/images.yml

	@echo "==> Pushing Carvel imgpkg bundle $(BUNDLE_IMAGE)..."
	@if [ -n "$(REGISTRY_USERNAME)" ] && [ -n "$(REGISTRY_PASSWORD)" ]; then \
		imgpkg push -b $(BUNDLE_IMAGE) -f .build/carvel/ --registry-username "$(REGISTRY_USERNAME)" --registry-password "$(REGISTRY_PASSWORD)"; \
	else \
		imgpkg push -b $(BUNDLE_IMAGE) -f .build/carvel/; \
	fi
	@echo "==> Success! Bundle $(BUNDLE_IMAGE) pushed successfully."

release-package: release-bundle
	@echo "==> Generating OpenAPI schema..."
	ytt -f deploy/carvel/config/values.yml --data-values-schema-inspect -o openapi-v3 > outputs/schema-openapi.yaml
	@echo "==> Generating Carvel Package Manifest..."
	ytt -f deploy/carvel/package-template.yaml \
		--data-value-file openapi=outputs/schema-openapi.yaml \
		-v version="$(VERSION)" \
		-v packagename="$(PACKAGE_NAME)" \
		-v imagepath="$(REGISTRY)/$(REPO)/$(BUNDLE_NAME)" \
		> deploy/carvel/repo/packages/capi-argocd-sync/$(VERSION).yaml
	@echo "==> Success! Artifact outputs/package-$(APP_NAME).yaml is ready."

all: release-package release-repo

clean:
	@echo "==> Cleaning generated files..."
	rm -f outputs/schema-openapi.yaml outputs/package-$(APP_NAME).yaml
	rm -f deploy/carvel/.imgpkg/images.yml

generate-base:
	@echo "==> Generating plain Kubernetes manifests from Carvel templates..."
	mkdir -p deploy/base
	ytt -f deploy/carvel/config/deploy.yaml -f deploy/carvel/config/values.yml -v image=$(APP_IMAGE_LATEST) > deploy/base/generated-manifests.yaml
	@echo "==> Success!  Base manifests generated in deploy/base/generated-manifests.yaml"

release-repo: release-package
	@echo "==> Resolving image digests with kbld..."
	mkdir -p deploy/carvel/repo/.imgpkg/
	kbld -f deploy/carvel/repo/packages --imgpkg-lock-output deploy/carvel/repo/.imgpkg/images.yml

	@echo "==> Pushing Carvel PackageRepository bundle $(REPO_IMAGE)..."
	@if [ -n "$(REGISTRY_USERNAME)" ] && [ -n "$(REGISTRY_PASSWORD)" ]; then \
		imgpkg push -b $(REPO_IMAGE) -f deploy/carvel/repo/ --registry-username "$(REGISTRY_USERNAME)" --registry-password "$(REGISTRY_PASSWORD)"; \
	else \
		imgpkg push -b $(REPO_IMAGE) -f deploy/carvel/repo/; \
	fi
	@echo "Success! PackageRepository $(REPO_IMAGE) pushed successfully."