.PHONY: help build up down restart logs ps test reentry windows program-closed session-dies ime window-bar pointer buttons clean session-image refresh-web

SESSION_IMAGE ?= portos/session:latest
COMPOSE       ?= docker compose

help: ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: session-image ## 构建镜像（session + gateway）
	$(COMPOSE) build gateway

session-image: ## 构建会话镜像（包含 Wine / Xvnc / noVNC）
	docker build -f session/Dockerfile -t $(SESSION_IMAGE) .

up: ## 启动网关并读取 Linux 宿主机时区
	@host_tz=$$(readlink -f /etc/localtime 2>/dev/null | sed -n 's|^/usr/share/zoneinfo/||p'); \
	if [ -z "$$host_tz" ]; then host_tz=$$(timedatectl show --property=Timezone --value 2>/dev/null); fi; \
	if [ -z "$$host_tz" ] && [ -r /etc/timezone ]; then host_tz=$$(cat /etc/timezone); fi; \
	PORTOS_HOST_TZ="$$host_tz" $(COMPOSE) up -d --remove-orphans gateway

down: ## 停止整套服务（会话容器也会一并结束）
	$(COMPOSE) down --remove-orphans

restart: ## 重启服务
	$(COMPOSE) restart

logs: ## 查看日志
	$(COMPOSE) logs -f

ps: ## 查看当前状态
	$(COMPOSE) ps
	@echo
	@echo "live sessions:"
	@docker ps --filter label=portos.session=1 --format '  {{.Names}}\t{{.Status}}'

test: ## 端到端验证（要求 1-4）
	$(COMPOSE) exec -T gateway python /app/scripts/verify.py --url http://gateway:8081

reentry: ## 回归：关掉浏览器再回来，必须拿到全新的会话
	$(COMPOSE) exec -T gateway python /app/scripts/test-reentry.py

windows: ## 回归：新开一个窗口，原来的窗口关掉，环境不能被带走（需要浏览器）
	$(COMPOSE) exec -T gateway python /app/scripts/window-reentry.py

program-closed: ## 回归：关掉程序，网页要说明白为什么结束了（需要浏览器）
	$(COMPOSE) exec -T gateway python /app/scripts/closing-program.py

session-dies: ## 回归：环境突然没了，网页要自己换一个（需要浏览器）
	$(COMPOSE) exec -T gateway python /app/scripts/session-dies.py

ime: ## 回归：用访客自己的输入法打中文，文字要能进到程序里（需要浏览器）
	$(COMPOSE) exec -T gateway python3 /app/scripts/ime-test.py

window-bar: ## 回归：窗口被收起来之后，要能从底部的窗口栏找回来（需要浏览器）
	$(COMPOSE) exec -T gateway python /app/scripts/window-bar.py

pointer: ## 回归：访客自己的鼠标指针要跟着远程的形状变（需要浏览器）
	$(COMPOSE) exec -T gateway python3 /app/scripts/pointer-shape.py

buttons: ## 回归：结束会话要二次确认，手机上要有软键盘按钮（需要浏览器）
	$(COMPOSE) exec -T gateway python3 /app/scripts/buttons.py

clean: ## 停止服务并清理所有会话容器
	-docker rm -f $$(docker ps -aq --filter label=portos.session=1)
	$(COMPOSE) down --remove-orphans

refresh-web: ## 重建会话容器（改了 web/ 之后用：网页文件是在建容器时送进去的）
	-docker rm -f $$(docker ps -aq --filter label=portos.session=1)
	@echo "会话容器已清空，服务端会补上一个新的"
