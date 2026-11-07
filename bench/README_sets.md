# Тренировочные наборы задач (bench/sets)

Генератор задач в формате примеров организаторов (`data/examples/task-dir`), оценщик и вспомогательные скрипты.
Только стандартная библиотека Python (3.11+; проверено на 3.12 и 3.14, вывод побайтно совпадает).

## Готовые наборы

| набор | команда | состав |
|---|---|---|
| `examples` | `import_examples.py` | 8 примеров организаторов |
| `train_a` | `--n 200 --seed 1 --infeasible 0.2 --size mixed` | 4×50, 40 невыполнимых |
| `train_b` | `--n 100 --seed 2 --infeasible 0.2` | 4×25, 20 невыполнимых |
| `qual` | `--n 2 --seed 3 --infeasible 0 --mix math=0.5,search=0.5 --size small --easy` | 1 math + 1 search, простые |
| `large` | `--n 40 --seed 4 --size large --infeasible 0.2` | CSV 500–2000 строк, база знаний 100–200 файлов, 6–8 функций |

`alt100` создан не генератором (`bench/tools/import_alt_set.py`).

## Раскладка набора

```
<set>/tasks/<task_id>/task.json        task_id, task_type, instruction, reward (без ответов)
<set>/tasks/<task_id>/workspace/...    sales.csv | kb/*.md | mylib.py + test_mylib.py (у math нет)
<set>/keys.json                        {task_id: {type, reward, feasible, kind, check, expected, meta}}
<set>/hidden/<task_id>/test_hidden.py  coding: строгий скрытый тест (вне tasks/)
<set>/hidden/<task_id>/mylib_ref.py    coding: эталонное исправление
<set>/oracle_mock.json                 [[подстрока, ответ], ...] для mock-режима stub_proxy.py
<set>/manifest.json                    параметры генерации и счётчики по видам
```

Харнессу отдаётся `<set>/tasks`. `check`: `exact_int` | `exact_str` | `unittest`; у невыполнимых `expected = null`.
Номера `<type>_<NNNN>` сквозные, награда равномерно 1..5 и не зависит от сложности.
Служебные фразы инструкций совпадают с примерами слово в слово; оба примера coding генератор воспроизводит побайтно.

## Семейства (выполнимые задачи)

- **math**: склад, 2–7 шагов. Шаги из примеров (ships N per day for D days / receives K deliveries of M / split equally into W and keep one share / doubled by merging) и новые: потеря P% (округление вниз), рост P%, продажа половины (округление вверх), отправка S коробок в N магазинов, возвраты, утроение, поставки с браком, упаковка в ящики с остатком; изредка вопрос «сколько полных ящиков». `kind`: `split_double`, `ship_receive`, `mixed` (+`_ext`, если есть новые шаги).
- **data_analysis**: `sales.csv` (CRLF, как в примерах), 20–300 строк, в mixed ~15 % наборов 400–2000; иногда 5-й продукт или регион. Вопросы: `units_months` (как в примерах, ~50 %), `units_months_region`, `units_region`, `revenue_region`, `revenue_product`, `order_count`, `order_count_region`, `avg_price_floor`, `avg_price_round` (без ничьих .5), `max_units_region`. Ответ 0 не бывает.
- **search**: `kb/person_<first>_<last>.md` (Born/Employer/Mentor) и `kb/company_<name>.md` (Headquarters/Founded). 60 % баз как в примерах (10 компаний + 24–30 человек), остальные 20–200 файлов. Имена пересекаются (общие имена и фамилии); в ~20 % баз есть лишние поля (Role, Hometown, Industry). Вопросы: `born`, `employer` (1 шаг), `hq`, `founded`, `mentor_employer`, `mentor_born` (2 шага), `mentor_hq` (3 шага).
- **coding**: в `mylib.py` 3–8 функций из 34 шаблонов, в 1–4 из них внесены ошибки (35 видов: неверный оператор или сравнение, сдвиг на единицу, `sorted` вместо `reversed`, неверное начальное значение, неверный модуль, пропущен `abs`, неверный срез и т. п.). `test_mylib.py` в стиле примеров: один метод на функцию. В половине случаев входы канонические, как у организаторов (`[3, 9, 2]`, `'c a b'`…), иначе случайные. Скрытый тест: видимые случаи, граничные и случайные.

## Невыполнимые (доля `--infeasible`, поровну по типам и видам)

- math: `split_zero` (split into 0), `vague_quantity` (some/several/a few), `unknown_source` (поставка неизвестного размера), `negative_stock` (отгружают больше, чем есть), `stated_contradiction` (указан остаток, не совпадающий с расчётом).
- data_analysis: `absent_product`, `absent_region`, `absent_month` (месяцы 13+), `missing_column` (discount, rating, customers, shipping, year, profit, returns, tax).
- search: `person_absent` (имя и фамилия есть в базе, но не в этом сочетании), `company_file_missing`, `attr_not_in_kb` (email, телефон, вуз, CEO, выручка…), `attr_line_missing` (в нужном файле нет строки), `mentor_absent`.
- coding: `contradict_same_method`, `contradict_two_methods` (один и тот же вызов с разными ожидаемыми значениями), `impossible_property` (равенство вместе с противоречащим ему свойством), `missing_function` (тест функции, которой нет в mylib, с противоречием).

## Проверки при генерации

Каждый ответ пересчитывается независимым кодом: он разбирает записанную инструкцию регулярками и читает записанные файлы (CSV, kb/).
Для невыполнимых задач тот же код обязан вернуть «нерешаемо».
coding (`python3 -I` во временной папке):
- ошибочная mylib падает на видимом тесте;
- эталон проходит видимый и скрытый тесты;
- «подгонка под тест» (ошибочный код плюс подстановка ответов для видимых входов) проходит видимый тест, но падает на скрытом;
- у невыполнимых эталон падает, а без строки-противоречия проходит.

Генерация детерминирована: семена задач выводятся через SHA-256.
Ключи `oracle_mock.json` проверены на уникальность: каждый встречается только в тексте своей задачи.
Порядок поиска ключа для coding: строка ошибочной mylib, затем строка видимого теста, затем фрагмент из нескольких строк mylib.

## Команды

```bash
python3 bench/gen_tasks.py --out bench/sets/NAME --n 200 --seed 1 --infeasible 0.2 \
    [--mix math=0.25,data_analysis=0.25,search=0.25,coding=0.25] [--size small|mixed|large] [--easy]
python3 bench/import_examples.py --src data/examples/task-dir --out bench/sets/examples
python3 bench/grade.py --set bench/sets/NAME --run RUN_DIR [--stats stats.json] [--json out.json] [-v]
python3 bench/oracle_fill.py --set bench/sets/NAME --run RUN_DIR [--fresh] \
    [--mode oracle|overfit|wrong|none] [--where task|workspace|both] [--infeasible-answer TEXT]
```

`RUN_DIR` — папка с каталогами задач после прогона (`RUN_DIR/<task_id>`, допускается и `RUN_DIR/tasks/<task_id>`).

### Правила grade.py

- `answer.txt` ищется в каталоге задачи, затем в `workspace/`.
- `exact_int`: `strip` + `int()`; ответ вида `389.0` засчитывается, но выводится предупреждение (настоящий оценщик может быть строже).
- `exact_str`: `strip` + точное совпадение.
- `unittest`: ответ `DONE`; копия workspace прогона, исходный видимый тест из набора и скрытый тест должны пройти. Изменённый `test_mylib.py` считается жульничеством: 0 баллов.
- Невыполнимые задачи всегда дают 0; отдельно считается, оставил ли харнесс ответ (`inf_ans`).
- С `--stats` выводятся потраченные токены (поле `spent`) и баллы на 1000 токенов.

### Самопроверка (train_a)

| режим oracle_fill | результат grade.py |
|---|---|
| `oracle` | 479/479 (100 %) |
| `none` | 0 |
| `wrong` | 0 |
| `overfit` (negative control) | coding 0/40, у всех 40 пометка overfit |
| изменён тест | помечено TESTS MODIFIED |
| ответ `N.0` | засчитан с предупреждением |
