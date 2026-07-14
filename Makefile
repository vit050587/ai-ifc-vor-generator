.PHONY: help up down restart logs clean

help:
	@echo "Доступные команды:"
	@echo "  make up              - Запустить все сервисы"
	@echo "  make down            - Остановить все сервисы"
	@echo "  make restart         - Перезапустить все сервисы"
	@echo "  make logs            - Показать логи всех сервисов"
	@echo "  make clean           - Очистить volumes и остановить контейнеры"

up:
	docker-compose up -d --build
	docker-compose logs -f --tail=100

down:
	docker-compose down --remove-orphans

restart: down up

logs:
	docker-compose logs -f --tail=100

clean:
	docker-compose down -v
	@echo "Контейнеры остановлены и volumes удалены"
