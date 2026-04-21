import redis
import requests

def test_redis():
    print("Probando conexión a Redis...")
    try:
        # Nota: El host es 'redis', que es el nombre del servicio en docker-compose
        r = redis.Redis(host='redis', port=6379, decode_responses=True)
        r.ping()
        print("✅ Conexión a Redis exitosa. La caché está operativa.")
    except Exception as e:
        print(f"❌ Error conectando a Redis: {e}")

def test_polymarket_gamma():
    print("\nProbando conexión a la Gamma API de Polymarket...")
    try:
        # Buscamos eventos activos relacionados con deportes
        url = "https://gamma-api.polymarket.com/events?closed=false&limit=5"
        response = requests.get(url)
        
        if response.status_code == 200:
            events = response.json()
            print(f"✅ Conexión exitosa. Últimos eventos activos encontrados:")
            for event in events:
                print(f"   - {event.get('title', 'Sin título')}")
        else:
            print(f"❌ Error de API. Código de estado: {response.status_code}")
    except Exception as e:
        print(f"❌ Error en la llamada HTTP: {e}")

if __name__ == "__main__":
    print("--- INICIANDO DIAGNÓSTICO DE INFRAESTRUCTURA ---\n")
    test_redis()
    test_polymarket_gamma()
    print("\n--- DIAGNÓSTICO FINALIZADO ---")