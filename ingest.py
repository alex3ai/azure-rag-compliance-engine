"""
Pipeline de Ingestão RAG Auditável
====================================
✅ Chunking inteligente com overlap
✅ Metadados de auditoria completos
✅ Validação de qualidade de chunks
✅ Cache local para economia de custos
✅ Rate limiting para evitar throttling
"""

import os
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from dataclasses import dataclass

# --- IMPORTAÇÕES CORRIGIDAS ---
try:
    from dotenv import load_dotenv
    from tqdm import tqdm
    from langchain_community.document_loaders import PyPDFLoader
    # CORREÇÃO AQUI: Importação do pacote específico de text-splitters
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_openai import AzureOpenAIEmbeddings
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        SimpleField,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        VectorSearch,
        HnswAlgorithmConfiguration,
        VectorSearchProfile,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SearchIndex
    )
    from azure.core.credentials import AzureKeyCredential
except ImportError as e:
    print(f"❌ Erro de importação: {e}")
    print("Execute: pip install langchain-community langchain-text-splitters langchain-openai azure-search-documents azure-identity python-dotenv tqdm pypdf")
    exit(1)

# Carregar variáveis de ambiente do arquivo .env
load_dotenv()

# ==================== CONFIGURAÇÕES ====================
@dataclass
class Config:
    """Configurações centralizadas"""
    # Azure OpenAI
    openai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT")
    openai_key: str = os.getenv("AZURE_OPENAI_API_KEY")
    embedding_deployment: str = os.getenv("AZURE_OPENAI_DEPLOYMENT_EMBEDDING", "text-embedding-ada-002")
    openai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2023-05-15")
    
    # Azure AI Search
    search_endpoint: str = os.getenv("AZURE_SEARCH_ENDPOINT")
    search_key: str = os.getenv("AZURE_SEARCH_KEY")
    index_name: str = os.getenv("AZURE_SEARCH_INDEX_NAME", "compliance-docs-index")
    
    # Chunking (otimizado baseado em boas práticas)
    chunk_size: int = 1000  # Caracteres
    chunk_overlap: int = 200  # 20% overlap para contexto
    
    # Rate limiting (economia + avoid throttling)
    batch_size: int = 10  # Processar 10 chunks por vez
    rate_limit_delay: float = 1.0  # 1 segundo entre batches
    
    # Cache
    cache_dir: Path = Path(".cache")
    use_cache: bool = True
    
    def validate(self):
        """Valida configurações obrigatórias"""
        required = {
            "AZURE_OPENAI_ENDPOINT": self.openai_endpoint,
            "AZURE_OPENAI_API_KEY": self.openai_key,
            "AZURE_SEARCH_ENDPOINT": self.search_endpoint,
            "AZURE_SEARCH_KEY": self.search_key
        }
        
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"❌ Variáveis de ambiente faltando no arquivo .env: {', '.join(missing)}")
        
        # Criar diretório de cache
        if self.use_cache:
            self.cache_dir.mkdir(exist_ok=True)
        
        print("✅ Configurações validadas com sucesso!")

# ==================== GERENCIAMENTO DE CACHE ====================
class CacheManager:
    """Gerencia cache local para economizar custos de API"""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        
    def _get_cache_path(self, content: str, prefix: str = "chunk") -> Path:
        """Gera caminho único para o cache baseado em hash do conteúdo"""
        content_hash = hashlib.md5(content.encode()).hexdigest()
        return self.cache_dir / f"{prefix}_{content_hash}.json"
    
    def get_embedding(self, text: str) -> List[float] | None:
        """Recupera embedding do cache"""
        cache_path = self._get_cache_path(text, "emb")
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)['embedding']
        return None
    
    def save_embedding(self, text: str, embedding: List[float]):
        """Salva embedding no cache"""
        cache_path = self._get_cache_path(text, "emb")
        with open(cache_path, 'w') as f:
            json.dump({
                'text': text[:100],  # Preview
                'embedding': embedding,
                'timestamp': datetime.now().isoformat()
            }, f)
    
    def clear_cache(self):
        """Limpa todo o cache"""
        import shutil
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
            self.cache_dir.mkdir()
        print("🗑️ Cache limpo")

# ==================== QUALIDADE DE CHUNKS ====================
class ChunkQualityValidator:
    """Valida qualidade dos chunks gerados"""
    
    @staticmethod
    def validate_chunk(text: str, min_length: int = 100, max_length: int = 2000) -> tuple[bool, str]:
        """
        Valida qualidade de um chunk
        Returns: (is_valid, reason)
        """
        # 1. Verificar tamanho
        if len(text) < min_length:
            return False, f"Chunk muito pequeno ({len(text)} chars)"
        
        if len(text) > max_length:
            return False, f"Chunk muito grande ({len(text)} chars)"
        
        # 2. Verificar se não está vazio ou só espaços
        if not text.strip():
            return False, "Chunk vazio"
        
        # 3. Verificar se tem conteúdo significativo (não só números/símbolos)
        alphanumeric_ratio = sum(c.isalnum() for c in text) / len(text) if len(text) > 0 else 0
        if alphanumeric_ratio < 0.5:
            return False, f"Baixo conteúdo significativo ({alphanumeric_ratio:.1%})"
        
        # 4. Verificar truncamento (Opcional - apenas warning)
        # last_char = text.strip()[-1]
        # if last_char not in '.!?;:': pass
        
        return True, "OK"

# ==================== CRIAÇÃO DO ÍNDICE ====================
def create_or_update_index(config: Config):
    """Cria ou atualiza o índice no Azure AI Search"""
    
    print("\n📊 Configurando índice no Azure AI Search...")
    
    # Cliente de gerenciamento de índices
    index_client = SearchIndexClient(
        endpoint=config.search_endpoint,
        credential=AzureKeyCredential(config.search_key)
    )
    
    # Definição do schema (otimizado para RAG + auditoria)
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,  # text-embedding-ada-002
            vector_search_profile_name="vector-profile"
        ),
        # Metadados para auditoria
        SimpleField(name="source_file", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="page_number", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32),
        SimpleField(name="compliance_level", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="indexed_at", type=SearchFieldDataType.DateTimeOffset),
        SimpleField(name="file_hash", type=SearchFieldDataType.String, filterable=True),
        # Metadados de qualidade
        SimpleField(name="chunk_quality_score", type=SearchFieldDataType.Double),
    ]
    
    # Configuração de busca vetorial (HNSW para performance)
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="hnsw-config",
                parameters={
                    "m": 4,  # Economia: menor uso de memória
                    "efConstruction": 400,
                    "efSearch": 500,
                    "metric": "cosine"
                }
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="vector-profile",
                algorithm_configuration_name="hnsw-config"
            )
        ]
    )
    
    # Busca semântica (melhora relevância)
    semantic_config = SemanticConfiguration(
        name="semantic-config",
        prioritized_fields=SemanticPrioritizedFields(
            content_fields=[SemanticField(field_name="content")],
        )
    )
    
    semantic_search = SemanticSearch(configurations=[semantic_config])
    
    # Criar índice
    index = SearchIndex(
        name=config.index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search
    )
    
    try:
        result = index_client.create_or_update_index(index)
        print(f"✅ Índice '{result.name}' criado/atualizado com sucesso!")
    except Exception as e:
        print(f"❌ Erro ao criar índice: {str(e)}")
        raise

# ==================== PROCESSAMENTO DE DOCUMENTOS ====================
class DocumentProcessor:
    """Processa documentos com chunking inteligente"""
    
    def __init__(self, config: Config, cache_manager: CacheManager):
        self.config = config
        self.cache = cache_manager
        
        # Inicializa Embeddings
        print(f"🔌 Conectando ao Azure OpenAI Embeddings ({config.embedding_deployment})...")
        self.embeddings = AzureOpenAIEmbeddings(
            azure_deployment=config.embedding_deployment,
            openai_api_version=config.openai_api_version,
            azure_endpoint=config.openai_endpoint,
            api_key=config.openai_key
        )
        
        # Splitter otimizado (baseado em boas práticas)
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", ". ", ", ", " ", ""],
            keep_separator=True
        )
        
        self.validator = ChunkQualityValidator()
    
    def load_document(self, pdf_path: str) -> List[Dict[str, Any]]:
        """Carrega e processa um documento PDF"""
        
        print(f"\n📄 Processando: {os.path.basename(pdf_path)}")
        
        try:
            # 1. Carregar PDF
            loader = PyPDFLoader(pdf_path)
            pages = loader.load()
            print(f"   📑 {len(pages)} páginas carregadas")
        except Exception as e:
            print(f"   ❌ Erro ao ler PDF: {e}")
            return []
        
        # 2. Hash do arquivo para tracking
        with open(pdf_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()[:16]
        
        # 3. Chunking inteligente
        all_chunks = []
        valid_chunks = 0
        invalid_chunks = 0
        
        for page in pages:
            chunks = self.text_splitter.split_text(page.page_content)
            
            for chunk_idx, chunk_text in enumerate(chunks):
                # Validar qualidade
                is_valid, reason = self.validator.validate_chunk(chunk_text)
                
                if is_valid:
                    chunk_data = {
                        "content": chunk_text,
                        "source_file": os.path.basename(pdf_path),
                        "page_number": page.metadata.get('page', 0),
                        "chunk_index": chunk_idx,
                        "compliance_level": "CONFIDENTIAL",  # Configurável
                        "indexed_at": datetime.now().isoformat() + "Z",
                        "file_hash": file_hash,
                        "chunk_quality_score": 1.0,  # Pode ser refinado
                    }
                    all_chunks.append(chunk_data)
                    valid_chunks += 1
                else:
                    invalid_chunks += 1
                    # Opcional: printar apenas se quiser debug detalhado
                    # print(f"   ⚠️ Chunk inválido: {reason}")
        
        print(f"   ✅ {valid_chunks} chunks válidos | ❌ {invalid_chunks} chunks rejeitados")
        
        return all_chunks
    
    def generate_embeddings_batch(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Gera embeddings em batches com cache"""
        
        if not chunks:
            return []
            
        print(f"\n🧮 Gerando embeddings para {len(chunks)} chunks...")
        
        enriched_chunks = []
        cache_hits = 0
        api_calls = 0
        
        # Processar em batches (economia + rate limiting)
        for i in tqdm(range(0, len(chunks), self.config.batch_size), desc="Batches"):
            batch = chunks[i:i + self.config.batch_size]
            
            for chunk in batch:
                # Verificar cache primeiro
                cached_emb = self.cache.get_embedding(chunk['content'])
                
                if cached_emb:
                    chunk['content_vector'] = cached_emb
                    cache_hits += 1
                else:
                    try:
                        # Chamar API
                        embedding = self.embeddings.embed_query(chunk['content'])
                        chunk['content_vector'] = embedding
                        
                        # Salvar no cache
                        self.cache.save_embedding(chunk['content'], embedding)
                        api_calls += 1
                    except Exception as e:
                        print(f"❌ Erro na API de Embeddings: {e}")
                        continue
                
                # ID único
                chunk['id'] = f"{chunk['file_hash']}_{chunk['page_number']}_{chunk['chunk_index']}"
                
                enriched_chunks.append(chunk)
            
            # Rate limiting
            if i + self.config.batch_size < len(chunks):
                time.sleep(self.config.rate_limit_delay)
        
        print(f"   💾 Cache hits: {cache_hits} | 🌐 API calls: {api_calls}")
        print(f"   💰 Economia estimada: ${cache_hits * 0.0001:.4f}")  # ~$0.0001 por embedding
        
        return enriched_chunks

# ==================== INDEXAÇÃO ====================
def index_documents(config: Config, chunks: List[Dict[str, Any]]):
    """Indexa chunks no Azure AI Search"""
    
    if not chunks:
        print("⚠️ Nenhum chunk para indexar.")
        return

    print(f"\n📤 Indexando {len(chunks)} chunks no Azure AI Search...")
    
    search_client = SearchClient(
        endpoint=config.search_endpoint,
        index_name=config.index_name,
        credential=AzureKeyCredential(config.search_key)
    )
    
    # Indexar em batches
    batch_size = 100  # Azure AI Search recomenda até 1000
    
    for i in tqdm(range(0, len(chunks), batch_size), desc="Upload"):
        batch = chunks[i:i + batch_size]
        
        try:
            result = search_client.upload_documents(documents=batch)
            
            # Verificar resultados
            failed = [r for r in result if not r.succeeded]
            if failed:
                print(f"   ⚠️ {len(failed)} documentos falharam no batch {i//batch_size + 1}")
                
        except Exception as e:
            print(f"   ❌ Erro no batch {i//batch_size + 1}: {str(e)}")
            raise
    
    print(f"✅ Indexação concluída!")

# ==================== PIPELINE PRINCIPAL ====================
def main():
    """Pipeline principal de ingestão"""
    
    print("="*60)
    print("🚀 Pipeline de Ingestão RAG Auditável")
    print("="*60)
    
    # 1. Configuração
    try:
        config = Config()
        config.validate()
    except Exception as e:
        print(f"❌ Erro de Configuração: {e}")
        return
    
    cache_manager = CacheManager(config.cache_dir)
    try:
        processor = DocumentProcessor(config, cache_manager)
    except Exception as e:
        print(f"❌ Erro ao inicializar Processador: {e}")
        return
    
    # 2. Criar/Atualizar índice
    create_or_update_index(config)
    
    # 3. Processar documentos
    documents_dir = Path("documents")
    
    if not documents_dir.exists():
        print(f"\n⚠️ Pasta '{documents_dir}' não encontrada. Criando...")
        documents_dir.mkdir()
        print(f"➡️ Por favor, coloque seus arquivos PDF dentro da pasta '{documents_dir}' e execute novamente.")
        return
    
    pdf_files = list(documents_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"\n⚠️ Nenhum PDF encontrado em '{documents_dir}'. Adicione arquivos e tente novamente.")
        return
    
    print(f"\n📚 Encontrados {len(pdf_files)} PDFs para processar")
    
    all_chunks = []
    for pdf_path in pdf_files:
        chunks = processor.load_document(str(pdf_path))
        all_chunks.extend(chunks)
    
    print(f"\n📊 Total de chunks válidos: {len(all_chunks)}")
    
    # 4. Gerar embeddings
    enriched_chunks = processor.generate_embeddings_batch(all_chunks)
    
    # 5. Indexar
    index_documents(config, enriched_chunks)
    
    # 6. Estatísticas finais
    print("\n" + "="*60)
    print("✅ PIPELINE CONCLUÍDO COM SUCESSO!")
    print("="*60)
    print(f"📊 Estatísticas:")
    print(f"   • Documentos processados: {len(pdf_files)}")
    print(f"   • Chunks indexados: {len(enriched_chunks)}")
    print(f"   • Cache dir: {config.cache_dir}")
    print("\n🔍 Próximo passo: Testar busca com queries!")
    print("="*60)

if __name__ == "__main__":
    main()