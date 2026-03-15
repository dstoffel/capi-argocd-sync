# --- VARIABLES ---
VERSION      ?= 1.0.0
REGISTRY     ?= ghcr.io
REPO         ?= dstoffel
APP_NAME     ?= capi-argocd-sync
BUNDLE_NAME  ?= $(APP_NAME)-bundle
PACKAGE_NAME ?= capi-argocd-sync.kubetbx.io

APP_IMAGE    := $(REGISTRY)/$(REPO)/$(APP_NAME):$(VERSION)
BUNDLE_IMAGE := $(REGISTRY)/$(REPO)/$(BUNDLE_NAME):$(VERSION)

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
	@echo "==> Syncing image version in values.yml..."
	sed -i 's|image: .*|image: "$(APP_IMAGE)"|g' deploy/carvel/config/values.yml
	@echo "==> Building Docker image $(APP_IMAGE)..."
	docker build -t $(APP_IMAGE) src/
	@echo "==> Pushing Docker image $(APP_IMAGE)..."
	docker push $(APP_IMAGE)

release-bundle: release-image
	@echo "==> Resolving image digests with kbld (requires pushed image)..."
	mkdir deploy/carvel/.imgpkg
	kbld -f deploy/carvel/config/ --imgpkg-lock-output deploy/carvel/.imgpkg/images.yml
	@echo "==> Pushing Carvel imgpkg bundle $(BUNDLE_IMAGE)..."
	@if [ -n "$(REGISTRY_USERNAME)" ] && [ -n "$(REGISTRY_PASSWORD)" ]; then \
		imgpkg push -b $(BUNDLE_IMAGE) -f deploy/carvel/ --registry-username "$(REGISTRY_USERNAME)" --registry-password "$(REGISTRY_PASSWORD)"; \
	else \
		imgpkg push -b $(BUNDLE_IMAGE) -f deploy/carvel/; \
	fi

release-package: release-bundle
	@echo "==> Generating OpenAPI schema..."
	ytt -f deploy/carvel/config/values.yml --data-values-schema-inspect -o openapi-v3 > outputs/schema-openapi.yaml
	@echo "==> Generating Carvel Package Manifest..."
	ytt -f deploy/carvel/package-template.yaml \
		--data-value-file openapi=outputs/schema-openapi.yaml \
		-v version="$(VERSION)" \
		-v packagename="$(PACKAGE_NAME)" \
		-v imagepath="$(REGISTRY)/$(REPO)/$(BUNDLE_NAME)" \
		> outputs/package-$(APP_NAME).yaml
	@echo "==> Success! Artifact outputs/package-$(APP_NAME).yaml is ready."

all: release-package

clean:
	@echo "==> Cleaning generated files..."
	rm -f outputs/schema-openapi.yaml outputs/package-$(APP_NAME).yaml
	rm -f deploy/carvel/.imgpkg/images.yml

generate-base:
	@echo "==> Generating plain Kubernetes manifests from Carvel templates..."
	mkdir -p deploy/base
	ytt -f deploy/carvel/config/deploy.yaml -f deploy/carvel/config/values.yml > deploy/base/generated-manifests.yaml
	@echo "==> Success!  Base manifests generated in deploy/base/generated-manifests.yaml"