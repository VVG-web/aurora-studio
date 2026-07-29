---
title: "Промпт: оформить Decision Record"
template: Templates/decision_record_template.md
output: AuroraKnowledgeDB/Decisions/
skill: "/aurora-vault decide"
---

# Оформить Decision Record (ручной аналог `/aurora-vault decide`)

Скопируй в чат и заполни фигурные скобки:

---

Ты — системный аналитик проекта {{ПРОЕКТ}}. Оформи Decision Record по шаблону
`Templates/decision_record_template.md`.

Тема решения: {{тема}}

Контекст: {{зачем понадобилось решение, какие US/процессы затронуты}}

Варианты, которые обсуждались (перечисли ВСЕ, включая отклонённые, с причинами отказа):
1. {{вариант 1 — что, плюсы, минусы}}
2. {{вариант 2 — что, плюсы, минусы, почему отклонён}}

Что выбрали и почему: {{решение}}

Требования:
- Отклонённые варианты опиши так, чтобы через год был понятен ответ «почему не X».
- Если это решение заменяет прежнее — найди старый DR в AuroraKnowledgeDB/Decisions/,
  укажи его в supersedes и попроси меня подтвердить простановку ему status: superseded.
- Перечисли карточки AuroraKnowledgeDB, которые надо обновить из-за этого решения.
- Номер DR возьми следующим свободным из AuroraKnowledgeDB/Decisions/_index.md.
