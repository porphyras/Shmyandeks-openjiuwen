"""Synthetic tasks away from the bench templates (developer tool, not part of the image).

python3 -I bench/tools/make_div_syn.py bench/sets/div_syn

Knowledge bases with other card fields (films: Director/Release year/Genre/Studio; places:
Population/Mayor/Capital/Currency) and one in prose; tables with other schemas (';' separator,
decimal commas, dates as strings in two formats, a comment line before the header, two tables
to join); math word problems with other plots. Each group has infeasible tasks. Expected
answers are computed here, from the same data the files are written from."""
import csv
import io
import json
import os
import shutil
import sys

SEARCH_TAIL = (" Use the file tools or bash (ls, grep, cat) to find it. Work only inside the current directory. "
               "When you are done, write ONLY the final answer to the file answer.txt in the current directory "
               "(for example with bash: echo 42 > answer.txt).")
DATA_TAIL = (" Use bash with python3 to compute it. Work only inside the current directory. When you are done, "
             "write ONLY the final answer to the file answer.txt in the current directory (for example with bash: "
             "echo 42 > answer.txt).")
MATH_TAIL = (" Write ONLY the final answer (a number) to the file answer.txt in the current directory "
             "(for example with bash: echo 42 > answer.txt).")
KB = "The directory kb/ contains a small knowledge base of markdown files. "

FILMS = [
    ("The Silent River", "Mara Linde", 1994, "drama", "Northlight Pictures"),
    ("Glass Harbor", "Oskar Venn", 2003, "thriller", "Bluegate Studios"),
    ("Paper Kingdom", "Mara Linde", 1999, "fantasy", "Northlight Pictures"),
    ("Iron Meadow", "Lucia Ferro", 2011, "western", "Redrock Films"),
    ("The Last Lantern", "Oskar Venn", 2015, "drama", "Bluegate Studios"),
    ("Winter Signal", "Mara Linde", 2008, "thriller", "Redrock Films"),
    ("Salt and Copper", "Lucia Ferro", 2019, "comedy", "Northlight Pictures"),
]
DIRECTORS = [("Mara Linde", 1950, "Swedish"), ("Oskar Venn", 1962, "Danish"), ("Lucia Ferro", 1971, "Italian")]

PLACES = [  # city, country, population, mayor (None = not stated), founded
    ("Lumen City", "Ardenia", 412000, "Tomas Brecht", 1203),
    ("Port Vey", "Ardenia", 88000, None, 1460),
    ("Kessel", "Morvan", 157000, "Ilsa Moreau", 1122),
    ("Dunholt", "Morvan", 64000, "Pieter Saal", 1588),
]
COUNTRIES = [("Ardenia", "Lumen City", "ard", "Ardenic"), ("Morvan", "Kessel", "morv", "Morvish")]


def task(root, tid, instr, files, key):
    d = os.path.join(root, "tasks", tid)
    os.makedirs(os.path.join(d, "workspace"), exist_ok=True)
    for rel, text in files.items():
        p = os.path.join(d, "workspace", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    kind = key["type"]
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump({"task_id": tid, "task_type": kind, "reward": key["reward"], "instruction": instr}, f)
    return tid, key


def film_kb():
    files = {}
    for t, d, y, g, s in FILMS:
        files["kb/film_%s.md" % t.lower().replace(" ", "_")] = (
            "# %s\n\nDirector: %s\nRelease year: %d\nGenre: %s\nStudio: %s\n" % (t, d, y, g, s))
    for n, b, nat in DIRECTORS:
        files["kb/person_%s.md" % n.lower().replace(" ", "_")] = "# %s\n\nBorn: %d\nNationality: %s\n" % (n, b, nat)
    return files


def place_kb():
    files = {}
    for c, co, pop, mayor, fd in PLACES:
        lines = ["Country: %s" % co, "Population: %d" % pop] + (["Mayor: %s" % mayor] if mayor else []) + ["Founded: %d" % fd]
        files["kb/city_%s.md" % c.lower().replace(" ", "_")] = "# %s\n\n%s\n" % (c, "\n".join(lines))
    for n, cap, cur, lang in COUNTRIES:
        files["kb/country_%s.md" % n.lower()] = ("# %s\n\nCapital: %s\nCurrency: %s\nOfficial language: %s\n"
                                                % (n, cap, cur, lang))
    return files


def prose_kb():
    return {
        "kb/harbor_towns.md": "# Harbor towns of the north coast\n\nPort Vey is a harbor town in Ardenia. "
        "Since 2019 the town has been governed by mayor Ilsa Brandt, a former ferry captain.\n\n"
        "Kessel lies inland; its old harbor district was closed in 1931.",
        "kb/ferries.md": "# Ferry lines\n\nThe Vey-Kessel line was opened in 1987 by the shipping company Saltwind. "
        "Saltwind is headquartered in Dunholt.",
        "kb/notes.md": "# Council notes\n\nThe council of Dunholt approved a new library in 2021.",
    }


def sc(rows, sep=",", header=None, comment=None):
    buf = io.StringIO()
    if comment:
        buf.write(comment + "\n")
    w = csv.writer(buf, delimiter=sep, lineterminator="\n")
    if header:
        w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def main():
    root = sys.argv[1]
    if os.path.isdir(root):
        shutil.rmtree(root)
    keys = {}

    def add(tid, instr, files, typ, reward, expected, check="exact_str"):
        k = {"type": typ, "reward": reward, "feasible": expected is not None, "check": check, "expected":
             None if expected is None else str(expected)}
        keys.update([task(root, tid, instr, files, k)])

    fk, pk, pr = film_kb(), place_kb(), prose_kb()
    film = {t: (d, y, g, s) for t, d, y, g, s in FILMS}
    born = {n: b for n, b, _ in DIRECTORS}
    add("search_f01", KB + "Who directed The Silent River?" + SEARCH_TAIL, fk, "search", 1, "Mara Linde")
    add("search_f02", KB + "In which year was Glass Harbor released?" + SEARCH_TAIL, fk, "search", 1, 2003)
    add("search_f03", KB + "What genre is Iron Meadow?" + SEARCH_TAIL, fk, "search", 1, "western")
    add("search_f04", KB + "In which year was the director of Glass Harbor born?" + SEARCH_TAIL, fk, "search", 3,
        born[film["Glass Harbor"][0]])
    add("search_f05", KB + "Which studio produced The Last Lantern?" + SEARCH_TAIL, fk, "search", 1, "Bluegate Studios")
    add("search_f06", KB + "Who composed the soundtrack of The Silent River?" + SEARCH_TAIL, fk, "search", 2, None)
    add("search_f07", KB + "How many films in the knowledge base did Mara Linde direct?" + SEARCH_TAIL, fk, "search", 2,
        sum(1 for v in film.values() if v[0] == "Mara Linde"), "exact_int")
    add("search_f08", KB + "What is the nationality of the director of Salt and Copper?" + SEARCH_TAIL, fk, "search", 3,
        "Italian")
    pop = {c: p for c, _, p, _, _ in PLACES}
    add("search_c01", KB + "What is the population of Kessel?" + SEARCH_TAIL, pk, "search", 1, pop["Kessel"], "exact_int")
    add("search_c02", KB + "Who is the mayor of the capital of Ardenia?" + SEARCH_TAIL, pk, "search", 3, "Tomas Brecht")
    add("search_c03", KB + "Which currency is used in the country where Dunholt is located?" + SEARCH_TAIL, pk, "search",
        3, "morv")
    add("search_c04", KB + "Who is the mayor of Port Vey?" + SEARCH_TAIL, pk, "search", 2, None)
    add("search_c05", KB + "In which year was the capital of Morvan founded?" + SEARCH_TAIL, pk, "search", 3, 1122)
    add("search_c06", KB + "Which city has the larger population, Port Vey or Dunholt?" + SEARCH_TAIL, pk, "search", 2,
        "Port Vey")
    add("search_p01", KB + "Who is the mayor of Port Vey?" + SEARCH_TAIL, pr, "search", 2, "Ilsa Brandt")
    add("search_p02", KB + "In which town is the company that opened the Vey-Kessel ferry line headquartered?"
        + SEARCH_TAIL, pr, "search", 3, "Dunholt")
    add("search_p03", KB + "Who is the mayor of Dunholt?" + SEARCH_TAIL, pr, "search", 2, None)

    # Tables -------------------------------------------------------------------------------
    inv = [("A-%03d" % i, ["north", "south", "east"][i % 3], (i * 7) % 40 + 1, round(2.5 + (i * 13) % 50 / 4, 2),
            "2024-%02d-%02d" % (1 + i % 6, 1 + (i * 5) % 28)) for i in range(60)]
    inv_csv = sc([(a, b, c, ("%.2f" % d).replace(".", ","), e) for a, b, c, d, e in inv], ";",
                 ["sku", "warehouse", "qty", "unit_cost", "received"])
    files = {"inventory.csv": inv_csv}
    head = ("The file inventory.csv (separator ';', decimal comma) has columns sku;warehouse;qty;unit_cost;received "
            "(60 rows). ")
    add("data_d01", head + "What is the total quantity in warehouse 'north'?" + DATA_TAIL, files, "data_analysis", 1,
        sum(c for _, b, c, _, _ in inv if b == "north"), "exact_int")
    add("data_d02", head + "How many items (rows) were received in March 2024?" + DATA_TAIL, files, "data_analysis", 2,
        sum(1 for *_, e in inv if e.startswith("2024-03")), "exact_int")
    val = sum(c * d for _, b, c, d, _ in inv if b == "south")
    add("data_d03", head + "What is the total stock value (qty times unit_cost) in warehouse 'south'? Round to 2 "
        "decimal places." + DATA_TAIL, files, "data_analysis", 3, "%.2f" % val, "exact_num2")
    add("data_d04", head + "What is the average discount in warehouse 'east'?" + DATA_TAIL, files, "data_analysis", 2,
        None)

    emp = [("emp%02d" % i, ["R&D", "Sales", "Support"][i % 3], "%02d.%02d.%d" % (1 + i % 28, 1 + i % 12, 2015 + i % 8),
            40000 + (i * 1777) % 30000) for i in range(45)]
    tsv = sc(emp, "\t", ["name", "department", "hire_date", "salary"])
    head = "The file staff.tsv (tab-separated, dates as dd.mm.yyyy) has columns name, department, hire_date, salary (45 rows). "
    add("data_d05", head + "How many employees of department 'R&D' were hired before 2020?" + DATA_TAIL,
        {"staff.tsv": tsv}, "data_analysis", 2, sum(1 for _, d, h, _ in emp if d == "R&D" and int(h[-4:]) < 2020),
        "exact_int")
    add("data_d06", head + "What is the highest salary in department 'Support'?" + DATA_TAIL, {"staff.tsv": tsv},
        "data_analysis", 1, max(s for _, d, _, s in emp if d == "Support"), "exact_int")

    cust = [(100 + i, ["Oslo", "Bergen", "Tromso"][i % 3]) for i in range(20)]
    orders = [(5000 + j, 100 + (j * 7) % 20, 20 + (j * 37) % 200, ["paid", "refunded", "paid", "pending"][j % 4])
              for j in range(80)]
    city = dict(cust)
    files = {"customers.csv": sc(cust, ",", ["customer_id", "city"]),
             "orders.csv": sc(orders, ",", ["order_id", "customer_id", "amount", "status"],
                              comment="# export 2024-05-01, amounts in NOK")}
    head = ("The files customers.csv (customer_id, city) and orders.csv (order_id, customer_id, amount, status; its "
            "first line is a comment) describe a shop. ")
    add("data_d07", head + "What is the total amount of paid orders placed by customers from 'Oslo'?" + DATA_TAIL, files,
        "data_analysis", 3, sum(a for _, c, a, s in orders if s == "paid" and city[c] == "Oslo"), "exact_int")
    add("data_d08", head + "How many orders have status 'refunded'?" + DATA_TAIL, files, "data_analysis", 1,
        sum(1 for *_, s in orders if s == "refunded"), "exact_int")

    # Math -------------------------------------------------------------------------------
    for tid, text, ans, rw in [
        ("math_m01", "A tank holds 1200 liters of water. One pump drains 35 liters per minute while another fills "
                     "15 liters per minute. After how many minutes is the tank empty?", 60, 1),
        ("math_m02", "Anna is 4 times as old as her son. In 20 years she will be twice as old as he will be. How old "
                     "is Anna now?", 40, 2),
        ("math_m03", "A recipe needs 3 eggs for 8 pancakes. How many eggs are needed for 56 pancakes?", 21, 1),
        ("math_m04", "A cyclist rides 18 km at 12 km/h and then 30 km at 20 km/h. What is the total riding time in "
                     "minutes?", 180, 2),
        ("math_m05", "A shop sold some apples on Monday and 30 more apples on Tuesday than on Monday. How many apples "
                     "did it sell on Monday?", None, 2),
        ("math_m06", "The price of a jacket is reduced by 20% and then by a further 10%. The final price is 72 "
                     "dollars. What was the original price in dollars?", 100, 2),
    ]:
        add(tid, text + MATH_TAIL, {}, "math", rw, ans, "exact_int")
    with open(os.path.join(root, "keys.json"), "w") as f:
        json.dump(keys, f, indent=1)
    print("%d tasks in %s" % (len(keys), root))


main()
