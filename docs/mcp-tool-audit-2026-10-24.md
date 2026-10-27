# Аудит IncidentFlow MCP tools — 24 октября 2026

## Область и метод

Проверены все **52 инструмента**, доступные в этом чате для двух подключений: production (`mcp__incidentflow`, версия 1.0.39) и local (`mcp__incidentflow_local`, версия 0.1.0). Замер — один реальный вызов каждого инструмента в каждом окружении; некоторые независимые вызовы выполнялись параллельно. Время ниже — наблюдаемое время вызова из чата, включая транспорт и обработку; это **не** p95 или нагрузочный тест. Для `external_status_check` local сделан повторный вызов, тоже около 47.9 с.

Production workspace: `dserbenyukgood`; local workspace: `acme`. В production подключены Kubernetes, Grafana и Slack; Argo CD не подключён. В local эти четыре интеграции не подключены. Поэтому `INTEGRATION_UNAVAILABLE` для них ожидаем. Чтение Kubernetes и Grafana проверено на доступных production объектах. Записи в knowledge base не создавались: `knowledge_upsert` вызывался только с `dry_run=true`. Для investigation и Slack thread, где списки были пусты, проверен ответ на несуществующий ID, а не успешный сценарий.

Медиана по 52 вызовам: **production 3.29 с**, **local 0.88 с**. Максимум: **19.28 с** для production Kubernetes и **47.83 с** для local `external_status_check`. Медианы нельзя напрямую сравнивать как скорость серверов: доступность интеграций и сценарии различаются.

## Найденные проблемы

1. **P1 — ложный успех у 13 local Kubernetes tools.** При отключённом Kubernetes верхний ответ `status: "success"`, а внутри `data.status: "failed"` и JSON-строка с `INTEGRATION_NOT_CONNECTED`. Клиент, который проверяет только верхний статус, примет сбой за успех. Затронуты: `k8s_analyze_workload`, `k8s_cluster_overview`, `k8s_debug_pod`, `k8s_describe_pod`, `k8s_get_pod_logs`, `k8s_get_rollout_status`, `k8s_list_deployments`, `k8s_list_events`, `k8s_list_namespaces`, `k8s_list_pods`, `k8s_list_services`, `k8s_namespace_overview`, `k8s_show_unhealthy_pods`. Ожидаемый контракт: верхний `status: "error"` и `error.code: "INTEGRATION_UNAVAILABLE"`, как у `k8s_connection_health`.
2. **P1 — local `k8s_get_pod` неверно классифицирует тот же сбой.** Возвращает верхний `status: "error"`, но `error.code: "INTERNAL_ERROR"`; текст ошибки содержит `INTEGRATION_NOT_CONNECTED`. Это может запустить бесполезные повторы и скрыть нужное действие «подключить Kubernetes».
3. **P2 — local `external_status_check` медленно возвращает только диспетчеризацию.** Запрос `providers: ["github"], days_back: 1, wait_for_result: true` занял **47.83 с** и вернул `mode: "async", job_status: "dispatched"`. Повтор с `job_id` занял **47.89 с** и вернул тот же статус. Production с теми же параметрами завершился за **3.07 с**. Надо проверить worker/job polling и соблюдение `wait_for_result`; по этим двум вызовам причина не установлена.
4. **P2 — неверный код ошибки для отсутствующего Slack треда в production.** Интеграция Slack подключена, но `slack_alert_thread_get` и `incident_thread_summary` для заведомо несуществующего `message_ts` вернули `INTEGRATION_UNAVAILABLE` с сообщением `Slack API returned thread_not_found`. Код `NOT_FOUND` точнее и отличает отсутствие треда от отсутствия подключения.
5. **P3 — нет инструмента для создания расследования в текущем MCP наборе.** Доступны `investigation_list/get/continue/recheck`, но нет `investigation_start` или эквивалента. Оба списка пусты; поэтому фразу «давай начнём расследование» через этот набор инструментов выполнить нельзя. Успешный путь continue/recheck без существующего расследования тоже не проверен.
6. **Качество поиска требует отдельного теста релевантности.** Production `private_knowledge_search("OAuth consent")` вернул три документа, первым — план Stripe Billing/MCP со score 0.2317; очевидного ответа про consent в топ-3 нет. Это сигнал для оценки на наборе эталонных запросов, но одного запроса недостаточно, чтобы считать поиск сломанным.

### Что проверено как корректное

- OAuth авторизация активна в обоих подключениях; `incidentflow_auth_status` отвечает успешно.
- Production Kubernetes: pod `incidentflow-app-7dd98cfc7f-v4kgq` читается, `k8s_analyze_workload` считает deployment здоровым; список namespaces, pods, deployments, services, events и rollout возвращает структурированные данные. Самые долгие вызовы — `k8s_cluster_overview` и `k8s_connection_health`, оба около 19.28 с.
- Production Grafana: dashboard `incidentflow-mcp-observability` и панель 1 читаются; instant/range PromQL по разрешённому datasource возвращают данные. Ограничения `max_series` и `panel_limit` обозначены через `truncated`/warnings.
- `knowledge_upsert(dry_run=true)` в обоих окружениях ответил `validated=true, stored=false`.
- `external_status_check` production сообщил исторический инцидент «Grok 4.6 and Grok 4.7 Service Issues» под GitHub. Это **корректно**: запись есть в [официальном GitHub Status API](https://www.githubstatus.com/api/v2/incidents.json) и относится к Copilot AI Model Providers.
- `correlate_alerts` сгруппировал два синтетических алерта в один кластер в обоих окружениях.
- Отключённый Argo CD во всех девяти инструментах обоих окружений возвращает ожидаемый `INTEGRATION_UNAVAILABLE`.

## Результаты по каждому инструменту

Время округлено до сотых секунды. `OK` означает успешный верхний статус и отсутствие выявленного противоречия в проверенном сценарии; это не гарантия всех ветвей поведения.

| Инструмент | Production | Local | Оценка ответа |
| --- | --- | --- | --- |
| `analyze_dashboard_health` | 2.10 с · OK | 0.82 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `argocd_analyze_application` | 2.24 с · INTEGRATION_UNAVAILABLE | 1.27 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_connection_health` | 1.16 с · INTEGRATION_UNAVAILABLE | 0.38 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_find_recent_deployments` | 2.88 с · INTEGRATION_UNAVAILABLE | 0.92 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_get_application` | 2.88 с · INTEGRATION_UNAVAILABLE | 0.80 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_get_application_resources` | 1.80 с · INTEGRATION_UNAVAILABLE | 0.83 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_get_last_operation` | 1.49 с · INTEGRATION_UNAVAILABLE | 0.77 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_get_sync_history` | 1.60 с · INTEGRATION_UNAVAILABLE | 0.76 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `argocd_list_applications` | 1.23 с · INTEGRATION_UNAVAILABLE | 0.38 с · INTEGRATION_UNAVAILABLE | Ожидаемо: Argo CD не подключён в обоих окружениях. |
| `correlate_alerts` | 0.81 с · OK | 0.77 с · OK | 2 синтетических алерта сгруппированы в 1 кластер в обоих окружениях. |
| `external_status_check` | 3.07 с · completed | 47.83 с · async dispatched | ⚠️ Prod: завершённый ответ сверён с GitHub Status; local: 47.83 с до async/dispatched. |
| `grafana_connection_health` | 4.84 с · OK | 2.36 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_extract_panel_queries` | 1.59 с · OK | 0.89 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_get_dashboard` | 1.61 с · OK | 0.88 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_get_panel_view` | 4.21 с · OK | 0.94 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_list_dashboards` | 4.46 с · OK | 2.18 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_metrics_query` | 3.29 с · OK | 0.99 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `grafana_metrics_query_range` | 4.21 с · OK | 0.86 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: ожидаемое отсутствие Grafana. |
| `incident_summary` | 2.22 с · async dispatched | 1.43 с · not_found | Только negative path: prod вернул async job, local — not_found. |
| `incident_thread_summary` | 3.05 с · INTEGRATION_UNAVAILABLE | 1.11 с · INTEGRATION_UNAVAILABLE | ⚠️ Prod: несуществующий тред обозначен INTEGRATION_UNAVAILABLE; happy path не проверен. |
| `incidentflow_auth_status` | 3.08 с · OK | 1.43 с · OK | Успех в обоих окружениях. |
| `incidentflow_capabilities` | 3.09 с · OK | 1.18 с · OK | Успех в обоих окружениях. |
| `incidentflow_integrations_status` | 3.06 с · OK | 1.41 с · OK | Успех в обоих окружениях. |
| `integration_guide` | 1.65 с · OK | 0.87 с · OK | Успех в обоих окружениях. |
| `investigation_continue` | 3.29 с · NOT_FOUND | 1.06 с · NOT_FOUND | Только negative path: корректный NOT_FOUND для UUID; активных расследований нет. |
| `investigation_get` | 3.29 с · NOT_FOUND | 1.05 с · NOT_FOUND | Только negative path: корректный NOT_FOUND для UUID; активных расследований нет. |
| `investigation_list` | 3.50 с · OK | 2.56 с · OK | Успех, пустой список в обоих окружениях. |
| `investigation_recheck` | 3.29 с · NOT_FOUND | 1.05 с · NOT_FOUND | Только negative path: корректный NOT_FOUND для UUID; активных расследований нет. |
| `k8s_agent_status` | 3.59 с · OK | 0.95 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: корректная ошибка отсутствия Kubernetes. |
| `k8s_analyze_workload` | 7.15 с · OK | 0.58 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_cluster_overview` | 19.28 с · OK | 0.91 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_connection_health` | 19.28 с · OK | 0.91 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: корректная ошибка отсутствия Kubernetes. |
| `k8s_debug_pod` | 5.99 с · OK | 0.81 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_describe_pod` | 4.41 с · OK | 0.53 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_get_pod` | 4.11 с · OK | 0.50 с · INTERNAL_ERROR | ⚠️ Local: отсутствие интеграции ошибочно названо INTERNAL_ERROR. |
| `k8s_get_pod_logs` | 4.86 с · OK | 0.59 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_get_rollout_status` | 4.14 с · OK | 0.32 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_list_deployments` | 7.24 с · OK | 0.34 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_list_events` | 6.04 с · OK | 0.40 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_list_namespaces` | 15.66 с · OK | 0.38 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_list_pods` | 7.24 с · OK | 0.50 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_list_services` | 6.01 с · OK | 0.32 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_namespace_overview` | 8.11 с · OK | 0.34 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `k8s_rbac_check` | 7.40 с · OK | 0.37 с · INTEGRATION_UNAVAILABLE | Prod: рабочий ответ; local: корректная ошибка отсутствия Kubernetes. |
| `k8s_show_unhealthy_pods` | 5.53 с · OK | 0.33 с · ⚠️ inner failed | ⚠️ Prod: рабочий ответ; local: верхний success при вложенном failed. |
| `knowledge_get` | 0.68 с · found | 0.47 с · not_found | Prod: найден документ; local: ожидаемый not_found (другая база). |
| `knowledge_upsert` | 1.31 с · OK | 2.72 с · OK | Успешный dry_run, stored=false в обоих окружениях. |
| `mcp_version` | 3.12 с · OK | 1.16 с · OK | Успех в обоих окружениях. |
| `private_knowledge_search` | 1.45 с · OK | 1.05 с · OK | Prod: 3 результата с низкой релевантностью; local: пустая база. |
| `public_knowledge_search` | 2.02 с · OK | 1.32 с · OK | Prod: 3 релевантных результата; local: пустая база. |
| `slack_alert_thread_get` | 3.06 с · INTEGRATION_UNAVAILABLE | 0.75 с · INTEGRATION_UNAVAILABLE | ⚠️ Prod: несуществующий тред обозначен INTEGRATION_UNAVAILABLE; happy path не проверен. |
| `slack_alerts_list` | 5.54 с · OK | 1.49 с · INTEGRATION_UNAVAILABLE | Prod: пустой список; local: ожидаемое отсутствие Slack. |

## Ограничения и следующий шаг

Нет существующего investigation или Slack alert thread для проверки успешных сценариев. `incident_summary` для отсутствующего ID в production вернул async job, а local — `not_found`; завершение job здесь не подтверждено. Для последовательной проверки качества ответов нужен небольшой фиксированный набор реальных ID и эталонных ожиданий. Основной исправляемый дефект сейчас — единый верхний контракт ошибок Kubernetes; затем стоит проверить локальный worker для `external_status_check` и нормализацию ошибок Slack.
