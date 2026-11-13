VERSION := $(shell python3 src/tabspace.py --version)
DIST    := dist/tabspace-$(VERSION)

.PHONY: test integration dist install uninstall clean

test:            ## unit tests (no iTerm2 needed)
	python3 -m unittest discover -s tests

integration:     ## live test in a throwaway iTerm2 window (service must be running)
	python3 tests/integration.py

dist: test       ## build dist/tabspace-<version>.zip for sharing
	rm -rf $(DIST) $(DIST).zip
	mkdir -p $(DIST)/src
	cp install.sh uninstall.sh README.md LICENSE config.example.json $(DIST)/
	cp src/tabspace.py $(DIST)/src/
	cd dist && zip -qr tabspace-$(VERSION).zip tabspace-$(VERSION)
	@echo "Built $(DIST).zip"

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	rm -rf dist
