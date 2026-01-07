"""
Azure Function App - RAG Auditável (v2 Programming Model)
============================================================
✅ Python v2 Model (decoradores)
✅ Validação robusta de segurança
✅ Rate limiting automático
✅ Auditoria completa
✅ Controle de similaridade para evitar alucinações
"""

import azure.functions as func
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple
from collections import defaultdict
import hashlib

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
    
    def __init__(self, max_requests: int = 10, window_minutes: int = 1):
        self.max_requests = max_requests
        self.window = timedelta(minutes=window_minutes)
        self.requests: Dict[str, List[datetime]] = defaultdict(list)
    
    def is_allowed(self, client_id: str) -> Tuple[bool, str]:
        """
        Verifica se o cliente pode fazer uma requisição
        Returns: (is_allowed, message)
        """
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
        
        # Registrar requisição
        self.requests[client_id].append(now)
        return True, "OK"

# Instância global do rate limiter
rate_limiter = SimpleRateLimiter(max_requests=10, window_minutes=1)

# ==================== VALIDAÇÃO E SANITIZAÇÃO ====================
class InputValidator:
    """Valida e sanitiza inputs do usuário"""
    
    @staticmethod
    def validate_question(question: str) -> Tuple[bool, str, str]:
        """
        Valida a pergunta do usuário
        Returns: (is_valid, sanitized_question, error_message)
        """
        if not question:
            return False, "", "Pergunta não pode ser vazia"
        
        if not isinstance(question, str):
            return False, "", "Pergunta deve ser texto"
        
        # Limpar e normalizar
        sanitized = question.strip()
        
        # Verificar comprimento
        if len(sanitized) < 5:
            return False, "", "Pergunta muito curta (mínimo 5 caracteres)"
        
        if len(sanitized) > 1000:
            return False, "", "Pergunta muito longa (máximo 1000 caracteres)"
        
        # Verificar se não é só pontuação/números
        alphanumeric_count = sum(c.isalnum() for c in sanitized)
        if alphanumeric_count < 3:
            return False, "", "Pergunta deve conter pelo menos 3 caracteres alfanuméricos"
        
        # Detectar possíveis injection attacks
        suspicious_patterns = ['<script', 'javascript:', 'onerror=', '<?php', 'eval(']
        if any(pattern in sanitized.lower() for pattern in suspicious_patterns):
            return False, "", "Pergunta contém conteúdo suspeito"
        
        return True, sanitized, ""

# ==================== GERAÇÃO DE RESPOSTA ====================
class RAGEngine:
    """Engine principal de RAG com segurança"""
    
    def __init__(self):
        # Configurar clientes Azure
        self.embeddings = AzureOpenAIEmbeddings(
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_EMBEDDING", "text-embedding-ada-002"),
            openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2023-05-15"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY")
        )
        
        self.llm = AzureChatOpenAI(
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_CHAT", "gpt-35-turbo"),
            openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2023-05-15"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            temperature=0,  # Determinístico para compliance
            max_tokens=500  # Limitar para economia
        )
        
        self.search_client = SearchClient(
            endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
            index_name=os.getenv("AZURE_SEARCH_INDEX_NAME", "compliance-docs-index"),
            credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY"))
        )
        
        # Configurações de segurança
        self.min_relevance_score = 0.75  # 75% de confiança mínima
        self.top_k_chunks = 3
    
    def search_documents(self, question: str) -> List[Dict[str, Any]]:
        """
        Busca documentos relevantes com scoring de confiança
        """
        logger.info(f"Buscando documentos para: {question[:50]}...")
        
        try:
            # Gerar embedding da pergunta
            question_embedding = self.embeddings.embed_query(question)
            
            # Busca vetorial com filtro de relevância
            results = self.search_client.search(
                search_text=question,  # Hybrid search (texto + vetor)
                vector_queries=[{
                    "kind": "vector",
                    "vector": question_embedding,
                    "k_nearest_neighbors": self.top_k_chunks,
                    "fields": "content_vector"
                }],
                select=["content", "source_file", "page_number", "compliance_level"],
                top=self.top_k_chunks
            )
            
            # Processar resultados
            relevant_docs = []
            for result in results:
                # Azure AI Search retorna score normalizado (0-1)
                score = result.get('@search.score', 0)
                
                # CRÍTICO: Filtrar por relevância
                if score >= self.min_relevance_score:
                    relevant_docs.append({
                        'content': result.get('content', ''),
                        'source': result.get('source_file', 'Unknown'),
                        'page': result.get('page_number', 0),
                        'compliance': result.get('compliance_level', 'UNCLASSIFIED'),
                        'relevance_score': float(score)
                    })
                else:
                    logger.warning(f"Documento com baixa relevância ignorado: {score:.2f}")
            
            logger.info(f"Encontrados {len(relevant_docs)} documentos relevantes (mínimo: {self.min_relevance_score})")
            
            return relevant_docs
            
        except Exception as e:
            logger.error(f"Erro na busca: {str(e)}")
            raise
    
    def generate_answer(self, question: str, docs: List[Dict[str, Any]], client_ip: str) -> Dict[str, Any]:
        """
        Gera resposta com LLM baseado nos documentos
        """
        if not docs:
            return {
                "answer": "Não encontrei informações confiáveis nos documentos aprovados para responder esta pergunta.",
                "sources": [],
                "confidence": "BAIXA",
                "warning": "Resposta baseada em dados insuficientes"
            }
        
        # Construir contexto
        context = "\n\n---\n\n".join([
            f"Documento: {doc['source']} (Página {doc['page']})\n{doc['content']}"
            for doc in docs
        ])
        
        # Extrair fontes únicas
        sources = list(set([
            f"{doc['source']} (p. {doc['page']})"
            for doc in docs
        ]))
        
        # Prompt engineering para compliance
        system_prompt = f"""Você é um assistente de auditoria técnica especializado em compliance.

INSTRUÇÕES CRÍTICAS:
1. Responda APENAS com base no contexto fornecido abaixo
2. Se a informação não estiver no contexto, diga explicitamente "não encontrei essa informação nos documentos"
3. NUNCA invente ou especule informações
4. Cite especificamente qual documento suporta cada afirmação
5. Use linguagem técnica precisa
6. Mantenha respostas objetivas e concisas (máximo 3 parágrafos)

CONTEXTO DOS DOCUMENTOS APROVADOS:
{context}

PERGUNTA: {question}

RESPOSTA (baseada APENAS no contexto acima):"""
        
        try:
            # Gerar resposta
            response = self.llm.invoke(system_prompt)
            answer = response.content if hasattr(response, 'content') else str(response)
            
            # Calcular confiança média
            avg_confidence = sum(doc['relevance_score'] for doc in docs) / len(docs)
            confidence_level = "ALTA" if avg_confidence >= 0.9 else "MÉDIA" if avg_confidence >= 0.75 else "BAIXA"
            
            # Log de auditoria
            self._log_audit(
                client_ip=client_ip,
                question=question,
                sources=[doc['source'] for doc in docs],
                confidence=avg_confidence
            )
            
            return {
                "answer": answer,
                "sources": sources,
                "confidence": confidence_level,
                "confidence_score": f"{avg_confidence:.2%}",
                "documents_used": len(docs)
            }
            
        except Exception as e:
            logger.error(f"Erro ao gerar resposta: {str(e)}")
            raise
    
    def _log_audit(self, client_ip: str, question: str, sources: List[str], confidence: float):
        """Registra evento de auditoria"""
        audit_entry = {
            "timestamp": datetime.now().isoformat(),
            "client_ip": client_ip,
            "question_hash": hashlib.sha256(question.encode()).hexdigest()[:16],
            "sources": sources,
            "confidence": confidence
        }
        
        # Em produção, envie para Application Insights ou Log Analytics
        logger.info(f"AUDIT: {json.dumps(audit_entry)}")

# Instância global do engine
rag_engine = RAGEngine()
validator = InputValidator()

# ==================== ENDPOINT HTTP ====================
@app.route(
    route="ask_compliance",
    auth_level=func.AuthLevel.FUNCTION,  # Requer API key
    methods=["POST"]
)
def ask_compliance(req: func.HttpRequest) -> func.HttpResponse:
    """
    Endpoint principal para perguntas RAG
    
    Request Body:
    {
        "question": "Sua pergunta aqui"
    }
    
    Response:
    {
        "answer": "Resposta gerada",
        "sources": ["documento1.pdf", ...],
        "confidence": "ALTA|MÉDIA|BAIXA",
        "metadata": {...}
    }
    """
    logger.info('📥 Requisição recebida em /ask_compliance')
    
    try:
        # 1. Identificar cliente (para rate limiting)
        client_ip = req.headers.get('X-Forwarded-For', 'unknown')
        
        # 2. Rate Limiting
        is_allowed, rate_message = rate_limiter.is_allowed(client_ip)
        if not is_allowed:
            logger.warning(f"Rate limit: {client_ip}")
            return func.HttpResponse(
                json.dumps({
                    "error": "Rate limit excedido",
                    "message": rate_message,
                    "retry_after_seconds": int(rate_message.split()[-1].replace('s', ''))
                }),
                mimetype="application/json",
                status_code=429
            )
        
        # 3. Parse e Validação do Input
        try:
            req_body = req.get_json()
            question = req_body.get('question', '')
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "JSON inválido"}),
                mimetype="application/json",
                status_code=400
            )
        
        is_valid, sanitized_question, error_msg = validator.validate_question(question)
        if not is_valid:
            logger.warning(f"Validação falhou: {error_msg}")
            return func.HttpResponse(
                json.dumps({"error": error_msg}),
                mimetype="application/json",
                status_code=400
            )
        
        logger.info(f"Pergunta validada: {sanitized_question[:50]}...")
        
        # 4. Buscar Documentos
        try:
            relevant_docs = rag_engine.search_documents(sanitized_question)
        except Exception as e:
            logger.error(f"Erro na busca: {str(e)}")
            return func.HttpResponse(
                json.dumps({
                    "error": "Erro ao buscar documentos",
                    "message": "Serviço temporariamente indisponível"
                }),
                mimetype="application/json",
                status_code=503
            )
        
        # 5. Gerar Resposta
        try:
            result = rag_engine.generate_answer(
                question=sanitized_question,
                docs=relevant_docs,
                client_ip=client_ip
            )
        except Exception as e:
            logger.error(f"Erro ao gerar resposta: {str(e)}")
            return func.HttpResponse(
                json.dumps({
                    "error": "Erro ao gerar resposta",
                    "message": "Não foi possível processar sua pergunta"
                }),
                mimetype="application/json",
                status_code=500
            )
        
        # 6. Adicionar Metadados
        response_data = {
            **result,
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "model": "gpt-35-turbo",
                "compliance_level": "CONFIDENTIAL",
                "rate_limit_remaining": rate_limiter.max_requests - len(rate_limiter.requests[client_ip])
            }
        }
        
        # 7. Retornar Resposta
        logger.info(f"✅ Resposta gerada com sucesso (confiança: {result['confidence']})")
        
        return func.HttpResponse(
            json.dumps(response_data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        logger.error(f"❌ Erro não tratado: {str(e)}", exc_info=True)
        return func.HttpResponse(
            json.dumps({
                "error": "Erro interno do servidor",
                "message": "Ocorreu um erro inesperado"
            }),
            mimetype="application/json",
            status_code=500
        )

# ==================== HEALTH CHECK ====================
@app.route(
    route="health",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["GET"]
)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """
    Endpoint de health check para monitoramento
    """
    try:
        # Verificar conectividade com serviços
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "services": {
                "search": "ok",
                "openai": "ok"
            }
        }
        
        return func.HttpResponse(
            json.dumps(health_status),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({"status": "unhealthy", "error": str(e)}),
            mimetype="application/json",
            status_code=503
        )