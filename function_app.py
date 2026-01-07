"""
Azure Function App - RAG Auditável (v2 Programming Model)
============================================================
✅ Resilience Pattern: Fallback para modo offline se LLM falhar
✅ Validação robusta de segurança
✅ Rate limiting automático
✅ Auditoria completa
✅ Controle de similaridade (Ajustado para 0.01 - Captura Máxima)
"""

import azure.functions as func
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple
from collections import defaultdict
import hashlib

# Importações de IA
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

# Configurar logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Inicializar App
app = func.FunctionApp()

# ==================== RATE LIMITER ====================
class SimpleRateLimiter:
    """Rate limiter simples baseado em IP"""
    
    def __init__(self, max_requests: int = 20, window_minutes: int = 1):
        self.max_requests = max_requests
        self.window = timedelta(minutes=window_minutes)
        self.requests: Dict[str, List[datetime]] = defaultdict(list)
    
    def is_allowed(self, client_id: str) -> Tuple[bool, str]:
        now = datetime.now()
        # Limpar requisições antigas
        self.requests[client_id] = [
            req_time for req_time in self.requests[client_id]
            if now - req_time < self.window
        ]
        
        # Verificar limite
        if len(self.requests[client_id]) >= self.max_requests:
            oldest_request = min(self.requests[client_id])
            retry_after = (oldest_request + self.window - now).total_seconds()
            return False, f"Rate limit excedido. Tente novamente em {int(retry_after)}s"
        
        self.requests[client_id].append(now)
        return True, "OK"

rate_limiter = SimpleRateLimiter(max_requests=20, window_minutes=1)

# ==================== VALIDAÇÃO E SANITIZAÇÃO ====================
class InputValidator:
    """Valida e sanitiza inputs do usuário"""
    
    @staticmethod
    def validate_question(question: str) -> Tuple[bool, str, str]:
        if not question:
            return False, "", "Pergunta não pode ser vazia"
        if not isinstance(question, str):
            return False, "", "Pergunta deve ser texto"
        
        sanitized = question.strip()
        
        if len(sanitized) < 3:
            return False, "", "Pergunta muito curta"
        if len(sanitized) > 1000:
            return False, "", "Pergunta muito longa"
        
        return True, sanitized, ""

# ==================== GERAÇÃO DE RESPOSTA (COM FALLBACK) ====================
class RAGEngine:
    """Engine principal de RAG com segurança e Failover"""
    
    def __init__(self):
        # 1. Embeddings (Crítico - Deve funcionar)
        try:
            self.embeddings = AzureOpenAIEmbeddings(
                azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_EMBEDDING", "text-embedding-ada-002"),
                openai_api_version="2023-05-15",
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_API_KEY")
            )
        except Exception as e:
            logger.error(f"❌ Falha crítica ao iniciar Embeddings: {e}")
            raise

        # 2. LLM (Opcional - Pode falhar por cota/região)
        self.llm = None
        try:
            self.llm = AzureChatOpenAI(
                azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_CHAT", "gpt-35-turbo"),
                openai_api_version="2023-05-15",
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                temperature=0,
                max_tokens=500
            )
            logger.info("✅ LLM Inicializado com sucesso.")
        except Exception as e:
            logger.warning(f"⚠️ LLM falhou na inicialização (Modo Contingência Ativado): {e}")

        # 3. Search Client
        self.search_client = SearchClient(
            endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
            index_name=os.getenv("AZURE_SEARCH_INDEX_NAME", "compliance-docs-index"),
            credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY"))
        )
        
        # Threshold Mínimo (1%) para garantir retorno do Search
        self.min_relevance_score = 0.01 
        self.top_k_chunks = 5
    
    def search_documents(self, question: str) -> List[Dict[str, Any]]:
        """Busca documentos relevantes"""
        logger.info(f"🔍 Buscando documentos para: {question[:50]}...")
        
        try:
            # Gerar embedding
            question_embedding = self.embeddings.embed_query(question)
            
            # Busca Híbrida
            results = self.search_client.search(
                search_text=question,
                vector_queries=[{
                    "kind": "vector",
                    "vector": question_embedding,
                    "k_nearest_neighbors": self.top_k_chunks,
                    "fields": "content_vector"
                }],
                select=["content", "source_file", "page_number", "compliance_level"],
                top=self.top_k_chunks
            )
            
            relevant_docs = []
            logger.info(f"--- DEBUG BUSCA: '{question}' ---")
            
            for result in results:
                score = result.get('@search.score', 0)
                source = result.get('source_file', 'Unknown')
                page = result.get('page_number', 0)
                
                logger.info(f"   📄 Doc: {source} (p.{page}) | Score: {score:.4f}")
                
                if score >= self.min_relevance_score:
                    relevant_docs.append({
                        'content': result.get('content', ''),
                        'source': source,
                        'page': page,
                        'compliance': result.get('compliance_level', 'UNCLASSIFIED'),
                        'relevance_score': float(score)
                    })
            
            return relevant_docs
            
        except Exception as e:
            logger.error(f"Erro na busca: {str(e)}")
            raise
    
    def generate_answer(self, question: str, docs: List[Dict[str, Any]], client_ip: str) -> Dict[str, Any]:
        """Gera resposta com Circuit Breaker (Fallback se LLM falhar)"""
        
        sources = list(set([f"{doc['source']} (p. {doc['page']})" for doc in docs]))
        
        if not docs:
            return {
                "answer": "Nenhum documento encontrado (Verifique se o PDF foi indexado corretamente).",
                "sources": [],
                "confidence": "N/A"
            }
        
        # --- TENTATIVA DE USO DO LLM ---
        try:
            if not self.llm:
                raise Exception("LLM não foi inicializado corretamente.")

            logger.info("🧠 Enviando prompt para o LLM...")
            
            context = "\n\n---\n\n".join([f"Fonte: {d['source']}\n{d['content']}" for d in docs])
            
            system_prompt = f"""Você é um auditor de compliance.
            Use o contexto abaixo para responder à pergunta. Se não souber, diga "Não consta".
            
            CONTEXTO:
            {context}
            
            PERGUNTA: {question}"""
            
            response = self.llm.invoke(system_prompt)
            answer = response.content
            confidence_status = "ALTA"
            
        except Exception as e:
            # === MODO DE CONTINGÊNCIA (FALLBACK) ===
            logger.error(f"🔥 FALHA NO LLM (Ativando Fallback): {e}")
            
            # Monta resposta sintética com o conteúdo bruto
            top_content = docs[0]['content']
            source_ref = f"{docs[0]['source']} (p. {docs[0]['page']})"
            
            answer = (
                f"⚠️ **MODO DE CONTINGÊNCIA (IA Indisponível)**\n\n"
                f"O modelo de linguagem está indisponível na sua região Azure (Erro de Cota/Deploy).\n"
                f"Porém, o sistema localizou esta informação relevante no documento:\n\n"
                f"\"{top_content}...\"\n\n"
                f"📌 **Fonte:** {source_ref}"
            )
            confidence_status = "CONTINGÊNCIA"
            
        # Log de Auditoria
        self._log_audit(client_ip, question, sources, confidence_status)
        
        return {
            "answer": answer,
            "sources": sources,
            "confidence": confidence_status,
            "documents_used": len(docs)
        }
    
    def _log_audit(self, client_ip: str, question: str, sources: List[str], confidence: str):
        """Registra evento de auditoria"""
        audit_entry = {
            "timestamp": datetime.now().isoformat(),
            "client_ip": client_ip,
            "question_hash": hashlib.sha256(question.encode()).hexdigest()[:16],
            "sources": sources,
            "confidence": confidence
        }
        logger.info(f"AUDIT_EVENT: {json.dumps(audit_entry)}")

# Instância global
rag_engine = RAGEngine()
validator = InputValidator()

# ==================== ENDPOINT HTTP ====================
@app.route(route="ask_compliance", auth_level=func.AuthLevel.FUNCTION, methods=["POST"])
def ask_compliance(req: func.HttpRequest) -> func.HttpResponse:
    
    client_ip = req.headers.get('X-Forwarded-For', 'unknown')
    
    # Rate Limiting
    is_allowed, rate_msg = rate_limiter.is_allowed(client_ip)
    if not is_allowed:
        return func.HttpResponse(json.dumps({"error": rate_msg}), status_code=429)
    
    # Validar Input
    try:
        req_body = req.get_json()
        question = req_body.get('question', '')
    except ValueError:
        return func.HttpResponse(json.dumps({"error": "JSON inválido"}), status_code=400)
    
    is_valid, sanitized_q, error_msg = validator.validate_question(question)
    if not is_valid:
        return func.HttpResponse(json.dumps({"error": error_msg}), status_code=400)
    
    # RAG Pipeline com Tratamento de Erro Global
    try:
        relevant_docs = rag_engine.search_documents(sanitized_q)
        result = rag_engine.generate_answer(sanitized_q, relevant_docs, client_ip)
        
        response_data = {
            **result,
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "model": "gpt-fallback" if result['confidence'] == "CONTINGÊNCIA" else "gpt-standard"
            }
        }
        
        return func.HttpResponse(
            json.dumps(response_data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        logger.error(f"Erro crítico: {str(e)}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": "Erro interno do servidor", "details": str(e)}),
            status_code=500
        )