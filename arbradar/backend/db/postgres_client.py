import psycopg2

def get_connection():
    # Replace with your actual DB credentials
    return psycopg2.connect(
        dbname="arbradar",
        user="postgres",
        password="password",
        host="localhost",
        port=5432
    )
