"""Knowledge that is embedded into the vector database.

Three kinds of documents are stored and retrieved separately:
  * ``schema``    - tables, columns, and foreign keys (introspected live from PostgreSQL)
  * ``semantics`` - business rules and column meanings
  * ``example``   - few-shot (question, SQL) pairs
"""

TABLES = ("customers", "products", "orders", "order_items")

# Used only if live schema introspection fails (e.g. database temporarily unreachable).
FALLBACK_SCHEMA = {
    "customers": ["customer_id integer", "name text", "state text"],
    "products": ["product_id integer", "product_name text", "price numeric"],
    "orders": ["order_id integer", "customer_id integer", "status text"],
    "order_items": ["order_item_id integer", "order_id integer", "product_id integer", "quantity integer"],
}
FALLBACK_FKS = {
    "orders": ["customer_id -> customers(customer_id)"],
    "order_items": ["order_id -> orders(order_id)", "product_id -> products(product_id)"],
}

SEMANTICS = [
    ("revenue",
     "Revenue / total sales / sales amount for an order line is quantity * price, where quantity comes from "
     "order_items and price comes from products. Total revenue is SUM(order_items.quantity * products.price) "
     "by joining order_items to products on product_id. Revenue includes ALL orders, including Pending orders, "
     "unless the question explicitly restricts the order status."),
    ("spending",
     "A customer's spending (total spent, how much a customer spent) is SUM(order_items.quantity * products.price) "
     "over that customer's orders: customers -> orders (customer_id) -> order_items (order_id) -> products "
     "(product_id). Group by customers.customer_id and customers.name. Pending orders are included."),
    ("order status",
     "orders.status is text. Observed values are 'Completed' and 'Pending' (capitalised exactly like this). "
     "Use equality comparison with the exact capitalisation, e.g. status = 'Pending'."),
    ("state",
     "customers.state is a two-letter US state abbreviation in upper case, e.g. 'CA' for California and "
     "'NY' for New York. Translate full state names to their abbreviation."),
    ("price and products",
     "products.price is the unit price in US dollars (numeric). products.product_name is the display name of "
     "the product (there is no column called name in products). 'Expensive' means a higher price."),
    ("customer name",
     "customers.name is the customer's name (the column is called name, not customer_name)."),
    ("joins",
     "Join path: customers.customer_id = orders.customer_id; orders.order_id = order_items.order_id; "
     "order_items.product_id = products.product_id. Always qualify tables with the public schema or use plain "
     "table names, and use table aliases for joins."),
    ("output rules",
     "Only SELECT queries are allowed (WITH ... SELECT is fine). Never modify data. Use PostgreSQL syntax. "
     "Use LIMIT for 'top N' questions and ORDER BY ... DESC for highest/most."),
]

# Few-shot examples. They deliberately differ from the five quiz questions.
EXAMPLES = [
    ("List all products that cost more than 50 dollars.",
     "SELECT product_id, product_name, price FROM public.products WHERE price > 50 ORDER BY price DESC;"),
    ("How many customers live in each state?",
     "SELECT state, COUNT(*) AS customer_count FROM public.customers GROUP BY state ORDER BY customer_count DESC;"),
    ("Which customers have at least one Pending order?",
     "SELECT DISTINCT c.customer_id, c.name FROM public.customers c "
     "JOIN public.orders o ON o.customer_id = c.customer_id WHERE o.status = 'Pending';"),
    ("What is the total quantity sold for each product?",
     "SELECT p.product_name, SUM(oi.quantity) AS total_quantity FROM public.order_items oi "
     "JOIN public.products p ON p.product_id = oi.product_id GROUP BY p.product_name ORDER BY total_quantity DESC;"),
    ("Show the number of orders placed by each customer, including customers with no orders.",
     "SELECT c.customer_id, c.name, COUNT(o.order_id) AS order_count FROM public.customers c "
     "LEFT JOIN public.orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name "
     "ORDER BY c.customer_id;"),
    ("What is the average product price?",
     "SELECT AVG(price) AS average_price FROM public.products;"),
    ("Which product generated the most revenue?",
     "SELECT p.product_name, SUM(oi.quantity * p.price) AS revenue FROM public.order_items oi "
     "JOIN public.products p ON p.product_id = oi.product_id GROUP BY p.product_name "
     "ORDER BY revenue DESC LIMIT 1;"),
    ("List the customers who have never placed an order.",
     "SELECT c.customer_id, c.name FROM public.customers c "
     "WHERE NOT EXISTS (SELECT 1 FROM public.orders o WHERE o.customer_id = c.customer_id);"),
    ("What is the total revenue from Completed orders only?",
     "SELECT SUM(oi.quantity * p.price) AS completed_revenue FROM public.orders o "
     "JOIN public.order_items oi ON oi.order_id = o.order_id "
     "JOIN public.products p ON p.product_id = oi.product_id WHERE o.status = 'Completed';"),
    ("Show each order with the name of the customer who placed it.",
     "SELECT o.order_id, c.name, o.status FROM public.orders o "
     "JOIN public.customers c ON c.customer_id = o.customer_id ORDER BY o.order_id;"),
    ("Find the three cheapest products.",
     "SELECT product_name, price FROM public.products ORDER BY price ASC LIMIT 3;"),
    ("How many orders does each status have, only showing statuses with more than 2 orders?",
     "SELECT status, COUNT(*) AS order_count FROM public.orders GROUP BY status HAVING COUNT(*) > 2;"),
]

# Natural-language versions of the five Week 7 quiz questions with their reference SQL.
# Used by the optional result-validation feature (the reference SQL is never shown to the LLM).
QUIZ_QUESTIONS = [
    ("Find all customers who live in California.",
     "SELECT * FROM public.customers WHERE state = 'CA';"),
    ("Count the number of orders for each order status.",
     "SELECT status, COUNT(*) FROM public.orders GROUP BY status;"),
    ("What is the total sales revenue across all orders?",
     "SELECT SUM(oi.quantity * p.price) FROM public.order_items oi "
     "JOIN public.products p ON p.product_id = oi.product_id;"),
    ("Find the top three most expensive products.",
     "SELECT product_name, price FROM public.products ORDER BY price DESC LIMIT 3;"),
    ("Find customers who spent more than 100 dollars in total.",
     "SELECT c.name, SUM(oi.quantity * p.price) AS total_spent FROM public.customers c "
     "JOIN public.orders o ON o.customer_id = c.customer_id "
     "JOIN public.order_items oi ON oi.order_id = o.order_id "
     "JOIN public.products p ON p.product_id = oi.product_id "
     "GROUP BY c.customer_id, c.name HAVING SUM(oi.quantity * p.price) > 100;"),
]
