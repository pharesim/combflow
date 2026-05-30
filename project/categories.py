"""Category hierarchy for CombFlow content classification.

Two-level tree: top-level parents group related leaf categories.
Classification operates on leaf categories; subscriptions can target
either level (subscribing to a parent covers all its children).

No external dependencies — safe to import from the host seed script.
"""

# {parent_slug: [child_slugs]}
CATEGORY_TREE: dict[str, list[str]] = {
    "technology": [
        "crypto",           # blockchain, web3, DeFi
        "programming",      # absorbs devops, data-science
        "ai",
        "cybersecurity",
    ],
    "creative": [
        "photography",
        "art",              # absorbs digital-art, design
        "music",
        "writing",          # absorbs comedy
        "video",            # absorbs podcasts
        "diy-crafts",
    ],
    "lifestyle": [
        "travel",
        "food",
        "fashion",
        "homesteading",     # off-grid, farming, self-sufficiency
        "gardening",        # replaces home-garden, better Hive fit
        "pets",
    ],
    "science-education": [
        "nature",           # absorbs environment, outdoors
        "science",          # absorbs space
        "education",
        "health-fitness",   # merged health + fitness
    ],
    "society": [
        "politics",         # absorbs law
        "philosophy",       # absorbs psychology
        "history",
        "social-issues",    # absorbs religion
    ],
    "finance-business": [
        "finance",          # absorbs economics
        "entrepreneurship",
        "precious-metals",  # #silvergoldstackers, bullion, stacking
    ],
    "entertainment": [
        "gaming",           # absorbs esports, tabletop-games
        "movies-tv",        # absorbs anime-manga
        "books",
    ],
    "sports": [
        "team-sports",      # football, soccer, basketball, baseball, cricket, tennis
        "combat-sports",    # MMA, boxing, wrestling, martial arts, judo, karate
        "motorsports",      # Formula 1, NASCAR, rally, motocross, karting, drag racing
        "outdoor-sports",   # hiking, climbing, cycling, running, trail, surfing, skiing
        "chess",            # chess strategy, tournaments, puzzles, openings, endgames
    ],
    "community": [
        "hive",
        "contests",         # challenges, giveaways, contest posts
        "spirituality",     # meditation, mindfulness, astrology
    ],
}

# Flat list of all leaf categories (used for classification).
LEAF_CATEGORIES: list[str] = [
    cat for children in CATEGORY_TREE.values() for cat in children
]

# Parent names only.
PARENT_CATEGORIES: list[str] = list(CATEGORY_TREE.keys())

# Every slug — parents + leaves.
ALL_CATEGORIES: list[str] = PARENT_CATEGORIES + LEAF_CATEGORIES


# Human-written intro copy for each leaf category, keyed by leaf slug.
# Plain text only (no markdown) — the API HTML-escapes this before rendering it
# as the server-side intro paragraph on /c/{category} (proposal 100, task 1c).
# Each entry is unique and substantive (~150-300 chars) because it counts toward
# the Phase-2 visible-text floor that keeps those surfaces crawlable. Backend
# treats a missing/empty entry as "no intro paragraph" via .get(slug, ""), so a
# new leaf added before its copy exists never 500s — but aim for full coverage
# of LEAF_CATEGORIES (currently 38).
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    # technology
    "crypto": "Crypto and blockchain posts on Hive — Bitcoin and altcoin analysis, DeFi, NFTs, tokenomics, mining, and on-chain projects. Market takes, project deep-dives, and how-to guides from traders, builders, and long-time crypto enthusiasts.",
    "programming": "Programming and software development on Hive: coding tutorials, language deep-dives, open-source projects, web and app development, and dev tooling. Engineering write-ups from hobbyists and professionals sharing what they build.",
    "ai": "Artificial intelligence and machine learning on Hive — large language models, generative AI, neural networks, prompt engineering, and practical AI tools. Tutorials, experiments, and commentary on where the technology is heading.",
    "cybersecurity": "Cybersecurity and privacy on Hive: threat analysis, data protection, encryption, OPSEC, and wallet safety. Practical guides and news for anyone who wants to protect their accounts and identity online and in web3.",
    # creative
    "photography": "Photography on Hive — landscapes, street, portrait, macro, wildlife, and travel shots shared by photographers of every level. Galleries, editing walkthroughs, gear talk, and the stories behind the images.",
    "art": "Art and illustration on Hive: digital painting, traditional drawing, sketches, concept art, and mixed media. Artists share finished pieces, work-in-progress, process breakdowns, and the inspiration behind their creations.",
    "music": "Music on Hive — original songs, covers, instrumentals, production, and reviews. Musicians and listeners share performances, music theory, gear and software tips, and discoveries across every genre.",
    "writing": "Writing and poetry on Hive: short fiction, poems, essays, serialized stories, and creative prompts. A community of writers sharing their work, craft advice, and feedback for fellow authors.",
    "video": "Video and film on Hive — short films, vlogs, animation, video essays, and behind-the-scenes work. Creators share their productions along with editing techniques, gear, and storytelling tips.",
    "diy-crafts": "DIY and crafts on Hive: handmade projects, woodworking, sewing, knitting, upcycling, and home repairs. Step-by-step tutorials and project showcases from makers who love building things by hand.",
    # lifestyle
    "travel": "Travel on Hive — trip reports, destination guides, backpacking, city walks, and cultural discoveries from around the world. Photos, itineraries, and honest advice from travelers sharing their journeys.",
    "food": "Food and cooking on Hive: recipes, home cooking, baking, restaurant finds, and culinary experiments. Cooks of all kinds share dishes, techniques, and the stories behind their meals.",
    "fashion": "Fashion and beauty on Hive — outfits, style inspiration, makeup, skincare, and grooming. Looks, hauls, tutorials, and trend talk from creators sharing their personal style.",
    "homesteading": "Homesteading on Hive: off-grid living, self-sufficiency, raising animals, food preservation, and rural life. Practical guides and journals from people building a more independent, hands-on lifestyle.",
    "gardening": "Gardening on Hive — vegetable plots, flowers, houseplants, permaculture, and growing food at home. Planting guides, harvest updates, and tips from gardeners of every climate and skill level.",
    "pets": "Pets and animals on Hive: dogs, cats, birds, and exotic companions. Owners share photos, care tips, training advice, and the everyday adventures of life with their animals.",
    # science-education
    "nature": "Nature and the environment on Hive — wildlife, landscapes, conservation, climate, and the outdoors. Field notes, photography, and reflections from people who love and protect the natural world.",
    "science": "Science on Hive: physics, biology, chemistry, astronomy, and research explained for curious minds. Discoveries, deep-dives, and discussion that make complex ideas approachable.",
    "education": "Education and learning on Hive — study guides, teaching, languages, skill-building, and lifelong learning. Lessons, resources, and advice from educators and self-learners alike.",
    "health-fitness": "Health and fitness on Hive: workouts, nutrition, mental wellness, running, and training journeys. Routines, progress logs, and evidence-based advice for living a healthier life.",
    # society
    "politics": "Politics and current affairs on Hive — news analysis, policy debate, elections, and world events. A range of perspectives and commentary from people following the issues that shape society.",
    "philosophy": "Philosophy on Hive: ethics, metaphysics, logic, and the big questions about meaning and existence. Essays and discussion that explore ideas from ancient thinkers to modern debates.",
    "history": "History on Hive — events, figures, archaeology, and the stories behind how the world came to be. Well-researched articles and discussion that bring the past to life.",
    "social-issues": "Social issues and activism on Hive: human rights, equality, community organizing, and the causes people care about. Personal stories, analysis, and calls to action on the challenges facing society.",
    # finance-business
    "finance": "Finance and investing on Hive — personal finance, stocks, markets, budgeting, and building wealth. Strategies, analysis, and money lessons from investors and everyday savers.",
    "entrepreneurship": "Entrepreneurship on Hive: startups, small business, marketing, side hustles, and the founder journey. Lessons learned, growth tactics, and honest reflections from people building their own ventures.",
    "precious-metals": "Precious metals and stacking on Hive — gold, silver, and bullion collecting, coins, and tangible-asset investing. Stack photos, market commentary, and tips from a dedicated community of stackers.",
    # entertainment
    "gaming": "Gaming on Hive: PC, console, mobile, and blockchain games. Reviews, playthroughs, strategy guides, esports, and game design discussion from players and creators across every genre.",
    "movies-tv": "Movies and TV on Hive — reviews, recommendations, and discussion of films and series old and new. Reactions, analysis, and watch lists from fans of every genre.",
    "books": "Books and literature on Hive: reviews, reading lists, author spotlights, and discussion of fiction and non-fiction alike. Recommendations and reflections from a community of readers.",
    # sports
    "team-sports": "Team sports on Hive — football and soccer, basketball, baseball, cricket, and tennis. Match analysis, results, transfer news, and fan reactions covering leagues, clubs, and athletes around the world.",
    "combat-sports": "Combat sports on Hive: MMA, boxing, wrestling, and martial arts like judo and karate. Fight breakdowns, event recaps, training insights, and discussion from fans and practitioners of the fighting arts.",
    "motorsports": "Motorsports on Hive — Formula 1, NASCAR, rally, motocross, karting, and drag racing. Race recaps, technical analysis, driver and team news, and the adrenaline of competition on two wheels and four.",
    "outdoor-sports": "Outdoor and adventure sports on Hive: hiking, climbing, cycling, running, trail, surfing, and skiing. Trip reports, training, gear reviews, and the thrill of getting outside and pushing limits.",
    "chess": "Chess on Hive: openings, tactics, endgames, puzzles, and tournament coverage. Annotated games, strategy lessons, and discussion for players from curious beginners to seasoned competitors.",
    # community
    "hive": "Hive and web3 social on Hive — the blockchain itself, dApps, communities, witnesses, and decentralized social media. News, tutorials, and discussion about the ecosystem that powers it all.",
    "contests": "Contests and challenges on Hive: writing prompts, photo contests, community competitions, and creative challenges. Entries, announcements, and a fun way to take part in the Hive community.",
    "spirituality": "Spirituality on Hive — meditation, mindfulness, faith, energy work, and personal growth. Reflections and practices shared by people exploring meaning and inner well-being.",
}
