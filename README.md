# DB Health Check MySQL

Automação para padronizar a análise de ambientes MySQL e transformar dados dispersos em um relatório técnico organizado.

## 🎯 Objetivo

Reduzir o trabalho manual do DBA na consolidação de inventário, métricas de infraestrutura, objetos do banco e consultas lentas. O projeto recebe arquivos CSV ou TSV, normaliza os dados e gera relatórios HTML por camada e um relatório consolidado.

## ⚙️ Como funciona

A execução é configurada por instância e período em um arquivo `run.yaml`. O script `run_healthcheck.sh` prepara os diretórios, valida as entradas, executa os normalizadores em Python e gera os relatórios.

A análise está dividida em quatro camadas:

- **Camada 0 — Inventário:** identifica engine, versão, tipo de implantação e configurações de RDS MySQL ou Aurora MySQL.
- **Camada A — CloudWatch:** analisa CPU, carga relativa às vCPUs, conexões, memória, armazenamento, latência e IOPS.
- **Camada B — Objetos MySQL:** avalia tamanhos de bancos e tabelas, índices, fragmentação, varreduras completas e índices possivelmente não utilizados.
- **Camada C — Slow Log:** identifica consultas por tempo total, execuções, locks, pior tempo individual e linhas examinadas, além de análises por usuário e IP.

Ao final, o arquivo `healthcheck_full.html` reúne todas as camadas em uma única interface navegável.

## 🛠️ Tecnologias utilizadas

- Python 3 e Bash;
- MySQL, Amazon RDS, Aurora MySQL e AWS CloudWatch;
- Pandas, NumPy, Matplotlib, Jinja2 e PyYAML;
- HTML, CSS e JavaScript;
- CSV, TSV, YAML, Git e GitHub.

## 📁 Estrutura principal

- `scripts/layer0`: inventário;
- `scripts/layerA`: métricas do CloudWatch;
- `scripts/layerB`: objetos MySQL;
- `scripts/layerC`: Slow Log;
- `scripts/report`: relatórios HTML;
- `instances`: configurações, entradas, resultados e logs por ambiente.

## ▶️ Execução

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

bash scripts/run_healthcheck.sh \
  --run-yaml instances/<instancia>/run.yaml \
  --week YYYY-MM-DD
```

## ✅ Resultados e aprendizados

O projeto tornou o health check mais padronizado, rastreável e reutilizável. Seu desenvolvimento aprofundou meus conhecimentos em automação com Python e Bash, normalização e análise de dados, métricas de nuvem, configuração com YAML e geração de relatórios em HTML.

O principal aprendizado foi transformar atividades recorrentes de um DBA em um fluxo automatizado, separando claramente os dados de entrada, o processamento e a apresentação dos resultados.

## 🔗 Repositório

[github.com/rootdanley/db_healthcheck_mysql](https://github.com/rootdanley/db_healthcheck_mysql)
