.PHONY: lint dev lint-check unit-tests coverage html-cov build build-frontend compile-translations init-translation update-translations

lint:
	poetry run black .
	poetry run ruff check --fix .

dev: lint
	poetry run pyright .

lint-check:
	poetry run black --check .
	poetry run ruff check .
	poetry run pyright .
	poetry run interrogate platzky_telegram_notifier/ --verbose

unit-tests:
	poetry run python -m pytest -v

coverage:
	poetry run coverage run --branch --source=platzky_telegram_notifier -m pytest -m "not skip_coverage"
	poetry run coverage report --fail-under=90

html-cov: coverage
	poetry run coverage html

build: compile-translations
	poetry build

compile-translations:
	@if ls platzky_telegram_notifier/locale/*/LC_MESSAGES/*.po >/dev/null 2>&1; then \
		poetry run pybabel compile -d platzky_telegram_notifier/locale; \
	else \
		echo "No translation catalogs to compile; skipping"; \
	fi

init-translation:
	poetry run pybabel extract -F babel.cfg -o messages.pot \
		--project="platzky-telegram-notifier" \
		--version="$$(poetry version -s)" \
		platzky_telegram_notifier/
	poetry run pybabel init -i messages.pot -d platzky_telegram_notifier/locale -l $(LOCALE)

update-translations:
	poetry run pybabel extract -F babel.cfg -o messages.pot \
		--project="platzky-telegram-notifier" \
		--version="$$(poetry version -s)" \
		platzky_telegram_notifier/
	poetry run pybabel update -i messages.pot -d platzky_telegram_notifier/locale
