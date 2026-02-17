Adicionar instancia
INSTANCE=cliente-x

mkdir -p instances/$INSTANCE/{layer0,layerA,layerB,layerC}/{inputs,collected} \
         instances/$INSTANCE/{logs,tmp} \
         instances/$INSTANCE/reports/{figures,html}

touch instances/$INSTANCE/run.yaml
