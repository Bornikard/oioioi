"""Fixed tag list for problem descriptions.

Kept free of Django imports: the offline description tool (tools/describe.py)
loads this file directly.

Only leaf techniques are assigned to problems. Categories are not tags; they
group tags in the UI and in filters. A tag with a parent also counts as the
parent when filtering (``lazy-segment-tree`` finds ``segment-tree`` problems).

Sources: tag statistics of ~4200 Szkopuł problems analysed with an earlier
version of this list (generic tags such as "implementation", "math",
"graphs" ended up on a third of all problems and were dropped; dead tags were
merged), Codeforces tags, the USACO Guide module list and the IOI syllabus.
"""

VERSION = 1

CATEGORIES = [
    ("basics", "Podstawy"),
    ("techniques", "Techniki"),
    ("structures", "Struktury danych"),
    ("graphs", "Grafy"),
    ("trees", "Drzewa"),
    ("dp", "Programowanie dynamiczne"),
    ("strings", "Teksty"),
    ("math", "Matematyka"),
    ("geometry", "Geometria"),
]

# (slug, Polish name, category, parent slug or None, synonyms used for search and in the prompt)
TAGS = [
    # Podstawy
    ("simulation", "symulacja", "basics", None, ["symulacja procesu"]),
    ("heavy-implementation", "trudna implementacja", "basics", None, ["dużo przypadków", "żmudny kod"]),
    ("brute-force", "przegląd zupełny", "basics", None, ["brut", "sprawdzenie wszystkich możliwości"]),
    ("greedy", "algorytm zachłanny", "basics", None, ["zachłan", "greedy", "argument wymiany"]),
    ("sorting", "sortowanie", "basics", None, ["sortowanie z komparatorem"]),
    ("constructive", "konstrukcja", "basics", None, ["konstrukcyjne", "zbuduj przykład"]),
    ("ad-hoc", "obserwacja", "basics", None, ["ad hoc", "niezmiennik", "parzystość", "zasada szufladkowa"]),
    ("bit-manipulation", "operacje bitowe", "basics", None, ["xor", "maski bitowe"]),
    ("big-numbers", "duże liczby", "basics", None, ["arytmetyka dużych liczb", "bignum"]),
    ("interactive", "zadanie interaktywne", "basics", None, ["interaktywne", "biblioteka", "zapytania do sprawdzaczki"]),
    ("randomized", "algorytm randomizowany", "basics", None, ["losowość", "randomizacja", "haszowanie losowe"]),
    # Techniki
    ("prefix-sums", "sumy prefiksowe", "techniques", None, ["sumy częściowe", "tablica różnicowa", "sumy prefiksowe 2D"]),
    ("two-pointers", "gąsienica / dwa wskaźniki", "techniques", None, ["gąsienica", "dwa wskaźniki", "okno przesuwne"]),
    ("binary-search", "wyszukiwanie binarne", "techniques", None, ["binsearch", "bisekcja", "wyszukiwanie ternarne"]),
    ("binary-search-answer", "wyszukiwanie binarne po wyniku", "techniques", "binary-search", ["binsearch po wyniku"]),
    ("parallel-binary-search", "równoległe wyszukiwanie binarne", "techniques", "binary-search", ["parallel binary search"]),
    ("backtracking", "rekurencja z nawrotami", "techniques", None, ["nawroty", "backtracking", "przeszukiwanie z obcięciami"]),
    ("meet-in-the-middle", "meet in the middle", "techniques", None, ["spotkanie w środku"]),
    ("divide-and-conquer", "dziel i zwyciężaj", "techniques", None, ["divide and conquer", "dziel i rządź"]),
    ("sweep-line", "zamiatanie", "techniques", None, ["miotła", "sweep line"]),
    ("offline-queries", "zapytania offline", "techniques", None, ["przetwarzanie offline", "sortowanie zapytań"]),
    ("coordinate-compression", "kompresja współrzędnych", "techniques", None, ["przeskalowanie", "kompresja wartości"]),
    ("sqrt-decomposition", "pierwiastki", "techniques", None, ["dekompozycja pierwiastkowa", "algorytm Mo", "sqrt"]),
    ("small-to-large", "mniejszy do większego", "techniques", None, ["small to large", "łączenie zbiorów mniejszy do większego"]),
    # Struktury danych
    ("stack", "stos", "structures", None, []),
    ("monotonic-stack", "stos monotoniczny", "structures", "stack", ["najbliższy większy element"]),
    ("monotonic-queue", "kolejka monotoniczna", "structures", None, ["minimum na oknie", "deque"]),
    ("priority-queue", "kolejka priorytetowa", "structures", None, ["kopiec", "heap"]),
    ("set-map", "zbiory i mapy", "structures", None, ["set", "map", "drzewo BST z biblioteki", "tablica haszująca"]),
    ("dsu", "find-union", "structures", None, ["union-find", "zbiory rozłączne"]),
    ("segment-tree", "drzewo przedziałowe", "structures", None, ["drzewo punkt-przedział", "drzewo przedział-punkt"]),
    ("lazy-segment-tree", "drzewo przedziałowe z leniwą propagacją", "structures", "segment-tree", ["leniwa propagacja", "drzewo przedział-przedział"]),
    ("fenwick", "drzewo Fenwicka", "structures", None, ["drzewo potęgowe", "BIT"]),
    ("sparse-table", "sparse table", "structures", None, ["RMQ", "tablica rzadka"]),
    ("persistent", "struktury trwałe", "structures", None, ["trwałe drzewo przedziałowe"]),
    ("balanced-bst", "zrównoważone drzewa BST", "structures", None, ["treap", "drzewiec", "splay"]),
    ("trie", "drzewo trie", "structures", None, ["drzewo prefiksowe", "trie binarne"]),
    ("bitset", "bitset", "structures", None, ["optymalizacja bitsetem", "przyspieszenie /64"]),
    # Grafy
    ("dfs", "DFS", "graphs", None, ["przeszukiwanie w głąb", "spójne składowe"]),
    ("bfs", "BFS", "graphs", None, ["przeszukiwanie wszerz", "BFS 0-1", "BFS wielu źródeł"]),
    ("shortest-paths", "najkrótsze ścieżki", "graphs", None, ["Dijkstra", "Bellman-Ford", "Floyd-Warshall"]),
    ("mst", "minimalne drzewo rozpinające", "graphs", None, ["Kruskal", "Prim", "MST"]),
    ("topological-sort", "sortowanie topologiczne / DAG", "graphs", None, ["DAG", "graf acykliczny"]),
    ("scc", "silnie spójne składowe", "graphs", None, ["SCC", "Tarjan", "Kosaraju"]),
    ("bridges", "mosty i punkty artykulacji", "graphs", None, ["dwuspójne składowe", "punkty artykulacji"]),
    ("2-sat", "2-SAT", "graphs", "scc", ["2SAT"]),
    ("euler-path", "ścieżka / cykl Eulera", "graphs", None, ["cykl Eulera"]),
    ("bipartite", "graf dwudzielny", "graphs", None, ["dwukolorowanie"]),
    ("matching", "skojarzenia", "graphs", None, ["skojarzenie w grafie dwudzielnym", "turbo matching", "Hall"]),
    ("flows", "przepływy", "graphs", None, ["maksymalny przepływ", "minimalne cięcie", "Dinic"]),
    ("functional-graph", "graf funkcyjny", "graphs", None, ["każdy wierzchołek ma jedną krawędź wychodzącą", "cykle w permutacji"]),
    # Drzewa
    ("lca", "LCA / skoki potęgowe", "trees", None, ["najniższy wspólny przodek", "binary lifting", "jump pointers"]),
    ("euler-tour", "preorder / Euler tour", "trees", None, ["numeracja preorder", "poddrzewo jako przedział"]),
    ("centroid", "dekompozycja centroidalna", "trees", None, ["centroid"]),
    ("hld", "heavy-light decomposition", "trees", None, ["HLD", "heavy-light"]),
    # Programowanie dynamiczne
    ("dp", "programowanie dynamiczne", "dp", None, ["DP", "dynamik"]),
    ("knapsack", "problem plecakowy", "dp", "dp", ["plecak", "suma podzbioru"]),
    ("lis-lcs", "LIS / LCS", "dp", "dp", ["najdłuższy rosnący podciąg", "najdłuższy wspólny podciąg"]),
    ("dp-bitmask", "DP na maskach bitowych", "dp", "dp", ["DP po podzbiorach", "SOS DP"]),
    ("dp-trees", "DP na drzewie", "dp", "dp", ["programowanie dynamiczne na drzewie"]),
    ("rerooting", "DP po wszystkich korzeniach", "dp", "dp-trees", ["rerooting", "przekorzenianie"]),
    ("dp-intervals", "DP na przedziałach", "dp", "dp", ["DP przedziałowe"]),
    ("dp-digits", "DP po cyfrach", "dp", "dp", ["digit DP"]),
    ("dp-optimization", "optymalizacje DP", "dp", "dp", ["convex hull trick", "Li Chao", "Knuth", "aliens / WQS", "dziel i zwyciężaj w DP"]),
    # Teksty
    ("hashing", "haszowanie", "strings", None, ["hasze", "haszowanie napisów"]),
    ("kmp", "KMP / funkcja prefiksowa", "strings", None, ["funkcja prefiksowa", "prefikso-sufiks", "funkcja Z"]),
    ("palindromes", "palindromy", "strings", None, ["Manacher"]),
    ("suffix-structures", "struktury sufiksowe", "strings", None, ["tablica sufiksowa", "drzewo sufiksowe", "automat sufiksowy"]),
    ("aho-corasick", "Aho-Corasick", "strings", None, ["wiele wzorców"]),
    # Matematyka
    ("number-theory", "teoria liczb", "math", None, ["NWD", "podzielność", "liczby pierwsze", "sito Eratostenesa", "dzielniki"]),
    ("modular-arithmetic", "arytmetyka modularna", "math", None, ["modulo", "odwrotność modularna", "szybkie potęgowanie"]),
    ("combinatorics", "kombinatoryka", "math", None, ["zliczanie", "symbol Newtona", "dwumian"]),
    ("inclusion-exclusion", "zasada włączeń i wyłączeń", "math", "combinatorics", ["włączenia i wyłączenia"]),
    ("probability", "prawdopodobieństwo", "math", None, ["wartość oczekiwana"]),
    ("game-theory", "teoria gier", "math", None, ["gry dwuosobowe", "Sprague-Grundy", "strategia wygrywająca"]),
    ("matrices", "macierze", "math", None, ["potęgowanie macierzy", "eliminacja Gaussa"]),
    ("xor-basis", "baza XOR", "math", None, ["baza liniowa nad Z2", "eliminacja Gaussa mod 2"]),
    ("fft", "FFT / mnożenie wielomianów", "math", None, ["NTT", "splot"]),
    # Geometria
    ("geometry", "geometria obliczeniowa", "geometry", None, ["iloczyn wektorowy", "przecięcie odcinków", "pole wielokąta"]),
    ("convex-hull", "otoczka wypukła", "geometry", "geometry", []),
]

# What the problem is about, independent of the technique (1-2 per problem).
OBJECTS = [
    ("sequence", "ciąg / tablica"),
    ("permutation", "permutacja"),
    ("string", "napis"),
    ("graph", "graf"),
    ("tree", "drzewo"),
    ("grid", "plansza"),
    ("points", "punkty na płaszczyźnie"),
    ("intervals", "przedziały / odcinki"),
    ("numbers", "liczby"),
    ("game", "gra"),
    ("queries", "zapytania i modyfikacje"),
]

TAG_SLUGS = [slug for slug, *_ in TAGS]
TAG_NAMES = {slug: name for slug, name, *_ in TAGS}
TAG_CATEGORY = {slug: category for slug, _name, category, *_ in TAGS}
TAG_PARENT = {slug: parent for slug, _name, _category, parent, _syn in TAGS if parent}
CATEGORY_NAMES = dict(CATEGORIES)
OBJECT_SLUGS = [slug for slug, _name in OBJECTS]
OBJECT_NAMES = dict(OBJECTS)


def expand(tags):
    """Tags with all their ancestors."""
    result = set()
    for tag in tags:
        while tag:
            result.add(tag)
            tag = TAG_PARENT.get(tag)
    return result


def for_prompt():
    lines = []
    for category, category_name in CATEGORIES:
        lines.append(f"{category_name}:")
        for slug, name, tag_category, _parent, synonyms in TAGS:
            if tag_category == category:
                extra = f" (np. {', '.join(synonyms)})" if synonyms else ""
                lines.append(f"- {slug}: {name}{extra}")
    lines.append("")
    lines.append("Obiekty:")
    lines.extend(f"- {slug}: {name}" for slug, name in OBJECTS)
    return "\n".join(lines)
