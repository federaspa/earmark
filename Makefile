UV := uv run

.PHONY: test file-now audiorec menubar reset-test-vault install uninstall

test:
	$(UV) pytest -q

file-now:
	$(UV) python -m app.filer

audiorec:
	./recorder-swift/build.sh

menubar:
	$(UV) python -m app.menubar

# Recreate the test vault as a read-only copy of your vault (the one in config.toml, or VAULT=...).
VAULT ?= $(shell $(UV) python -c "from app import config; print(config.load()['vault']['path'])")
reset-test-vault:
	@case "$(VAULT)" in *test-vault*) echo "config.toml points at test-vault; run with VAULT=<your vault>"; exit 1;; esac
	rm -rf test-vault
	rsync -a --exclude .obsidian --exclude .trash "$(VAULT)/" test-vault/

AGENTS := io.github.earmark.filer io.github.earmark.recorder
LA := $(HOME)/Library/LaunchAgents

# Install/refresh both LaunchAgents (filer watching inbox/, menu-bar app at login), filling in
# this folder, your home folder and the uv path.
install:
	@test -n "$$(command -v uv)" || { echo "uv not found"; exit 1; }
	@for a in $(AGENTS); do \
		launchctl bootout gui/$$(id -u)/$$a 2>/dev/null || true; \
		sed -e "s|__ROOT__|$(CURDIR)|g" -e "s|__HOME__|$(HOME)|g" -e "s|__UV__|$$(command -v uv)|g" \
			launchd/$$a.plist > $(LA)/$$a.plist; \
		launchctl bootstrap gui/$$(id -u) $(LA)/$$a.plist; \
		echo "loaded $$a"; \
	done

uninstall:
	@for a in $(AGENTS); do \
		launchctl bootout gui/$$(id -u)/$$a 2>/dev/null || true; \
		rm -f $(LA)/$$a.plist; \
		echo "removed $$a"; \
	done
