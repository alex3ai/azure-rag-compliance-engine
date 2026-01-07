import os
import requests
import json
from dotenv import load_dotenv

# Carrega variáveis
load_dotenv()

def test_api_health():
    """Verifica se a API está de pé"""
    print("🏥 Testando Health Check...")
    # Ajuste a URL se estiver rodando local ou na nuvem
    url = "http://localhost:7071/api/health" 
    
    try:
        response = requests.get(url)
        if response.status_code == 200:
            print("✅ API Online!")
        else:
            print(f"❌ API com problemas: {response.status_code}")
    except Exception as e:
        print(f"❌ Falha na conexão: {str(e)}")

def test_rag_query(question):
    """Testa uma pergunta real ao RAG"""
    print(f"\n🤖 Perguntando: '{question}'")
    
    url = "http://localhost:7071/api/ask_compliance"
    payload = {"question": question}
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            
            # Validações SRE
            print("\n📊 Resultado do Teste:")
            print(f"   • Resposta: {data.get('answer')[:100]}...") # Preview
            print(f"   • Fontes Citadas: {data.get('sources')}")
            print(f"   • Confiança: {data.get('confidence_score')}")
            print(f"   • Modelo usado: {data.get('metadata', {}).get('model')}")
            
            if len(data.get('sources', [])) > 0:
                print("✅ SUCESSO: O sistema recuperou fontes!")
            else:
                print("⚠️ ALERTA: O sistema respondeu mas não achou fontes (Alucinação?)")
                
        else:
            print(f"❌ Erro na API: {response.text}")
            
    except Exception as e:
        print(f"❌ Erro crítico: {str(e)}")

if __name__ == "__main__":
    # 1. Teste de Conectividade
    test_api_health()
    
    # 2. Teste de Raciocínio (Use uma pergunta que existe no seu PDF de teste)
    test_rag_query("Quais são os requisitos de segurança para dados em repouso?")