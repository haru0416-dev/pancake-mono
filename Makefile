VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
PY := uv run python
J ?= 6

.PHONY: all sources mono prop check specimen tune dist woff2 images clean

all: mono prop check specimen

sources:
	bash scripts/fetch_sources.sh

mono: sources
	$(PY) scripts/build.py -j $(J)

prop:
	$(PY) scripts/build_prop.py -j $(J)

check:
	$(PY) scripts/check.py build/fonts/*/*.ttf

specimen:
	$(PY) scripts/specimen.py

tune:
	$(PY) scripts/tune.py -j $(J)

# 系統ごとに、フォント・ライセンス・README を zip にまとめる
dist:
	rm -rf dist && mkdir -p dist
	cd build/fonts/mono && zip -qj ../../../dist/PancakeMono-$(VERSION).zip *.ttf ../../../OFL.txt ../../../README.md
	cd build/fonts/mono-nf && zip -qj ../../../dist/PancakeMonoNF-$(VERSION).zip *.ttf ../../../OFL.txt ../../../README.md
	cd build/fonts/mono-jpdoc && zip -qj ../../../dist/PancakeMonoJPDOC-$(VERSION).zip *.ttf ../../../OFL.txt ../../../README.md
	cd build/fonts/prop && zip -qj ../../../dist/PancakeSans-$(VERSION).zip *.ttf ../../../OFL.txt ../../../README.md
	ls -lh dist

# Web 用に、ビルドしたフォントを build/woff2/ に woff2 で出す(字の間引きはしない)
woff2:
	$(PY) scripts/woff2.py -j $(J)

# README の画像を撮り直す(初回は uv run playwright install chromium-headless-shell)
images:
	$(PY) scripts/images.py

clean:
	rm -rf build dist
