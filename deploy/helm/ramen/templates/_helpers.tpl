{{- define "ramen.labels" -}}
app.kubernetes.io/name: ramen-console
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "ramen.selector" -}}
app.kubernetes.io/name: ramen-console
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{/* §38.2: `<repo>@<digest>` when a digest is given (it must be sha256:<64 hex>), else `<repo>:<tag>` — (list repo tag digest) */}}
{{- define "ramen.ref" -}}
{{- $repo := index . 0 -}}{{- $digest := (index . 2) | default "" | toString -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}{{- fail (printf "image digest %q is not sha256:<64 hex>" $digest) -}}{{- end -}}
{{ printf "%s@%s" $repo $digest }}
{{- else -}}
{{ printf "%s:%s" $repo ((index . 1) | toString) }}
{{- end -}}
{{- end -}}
{{- define "ramen.platform" -}}
{{- if eq .Values.provider "azure" -}}{{ printf "%s/ramen" .Values.azure.acr }}
{{- else if eq .Values.provider "aws" -}}{{ printf "%s.dkr.ecr.%s.amazonaws.com/ramen" (.Values.aws.account | toString) .Values.region }}
{{- else -}}{{ printf "%s-docker.pkg.dev/%s/ramen" .Values.region .Values.project }}
{{- end -}}
{{- end -}}
{{- define "ramen.image" -}}
{{ include "ramen.ref" (list (default (printf "%s/console" (include "ramen.platform" .)) .Values.image.repository) .Values.image.tag .Values.image.digest) }}
{{- end -}}
{{- define "ramen.workerImage" -}}
{{ include "ramen.ref" (list (printf "%s/worker" (include "ramen.platform" .)) .Values.image.tag .Values.image.workerDigest) }}
{{- end -}}
{{- define "ramen.roleArn" -}}
{{ default (printf "arn:aws:iam::%s:role/ramen/ramen-console" (.Values.aws.account | toString)) .Values.aws.roleArn }}
{{- end -}}
{{- define "ramen.gsa" -}}
{{ default (printf "ramen-console@%s.iam.gserviceaccount.com" .Values.project) .Values.serviceAccount.gsa }}
{{- end -}}
{{- define "ramen.secretName" -}}
{{ default "ramen-console" .Values.console.existingSecret }}
{{- end -}}

{{/* §24.2: "true" when a builder is configured — builder.enabled when set, else whether a (legacy) identity is given. */}}
{{- define "ramen.builder.identity" -}}
{{- if eq .Values.provider "azure" -}}{{ .Values.builder.clientId }}
{{- else if eq .Values.provider "aws" -}}{{ .Values.builder.roleArn }}
{{- else -}}{{ .Values.builder.gsa }}{{- end -}}
{{- end -}}
{{- define "ramen.builder.enabled" -}}
{{- if kindIs "invalid" .Values.builder.enabled -}}
{{- if include "ramen.builder.identity" . -}}true{{- end -}}
{{- else if .Values.builder.enabled -}}true{{- end -}}
{{- end -}}
{{/* §33.4a: the evals Jobs' cloud identity, when the provider's is given (terraform outputs evals_*) */}}
{{- define "ramen.evals.identity" -}}
{{- if eq .Values.provider "azure" -}}{{ .Values.evals.clientId }}
{{- else if eq .Values.provider "aws" -}}{{ .Values.evals.roleArn }}
{{- else -}}{{ .Values.evals.gsa }}{{- end -}}
{{- end -}}
{{/* §33.4a: where edge traffic reaches a ramen-system pod from — the provider's load-balancer ranges unless given */}}
{{- define "ramen.edgeCidrs" -}}
{{- if .Values.networkPolicy.edgeCidrs -}}{{ toJson .Values.networkPolicy.edgeCidrs }}
{{- else if eq .Values.provider "aws" -}}{{ toJson (.Values.networkPolicy.awsAlbCidrs | default (list .Values.networkPolicy.awsVpcCidr)) }}
{{- else if eq .Values.provider "azure" -}}[]
{{- else -}}{{ toJson (list "35.191.0.0/16" "130.211.0.0/22") }}{{- end -}}
{{- end -}}
{{/* D89: the console's RAMEN_AWS_ALB_CIDRS — awsAlbCidrs; before D89 a satellite install listed whole VPCs in
console.env.RAMEN_AWS_VPC_CIDR, then networkPolicy.awsVpcCidr (both deprecated, rendered as before) */}}
{{- define "ramen.awsAlbCidrs" -}}
{{- if .Values.networkPolicy.awsAlbCidrs -}}{{ join "," .Values.networkPolicy.awsAlbCidrs }}
{{- else -}}{{ .Values.console.env.RAMEN_AWS_VPC_CIDR | default .Values.networkPolicy.awsVpcCidr }}{{- end -}}
{{- end -}}
{{- define "ramen.edgeNamespaces" -}}
{{- if .Values.networkPolicy.edgeNamespaces -}}{{ toJson .Values.networkPolicy.edgeNamespaces }}
{{- else if eq .Values.provider "azure" -}}{{ toJson (list "envoy-gateway-system") }}
{{- else -}}[]{{- end -}}
{{- end -}}
{{- define "ramen.redis.enabled" -}}
{{- if .Values.redis.enabled -}}true{{- end -}}
{{- end -}}
{{/* the console gets RAMEN_REDIS_URL: the in-cluster Redis or a managed one (redis.externalUrl), never both */}}
{{- define "ramen.redis.configured" -}}
{{- if and .Values.redis.enabled .Values.redis.externalUrl -}}
{{- fail "redis.enabled and redis.externalUrl are both set: run the chart's Redis or point at a managed one, not both" -}}
{{- end -}}
{{- if or .Values.redis.enabled .Values.redis.externalUrl -}}true{{- end -}}
{{- end -}}
{{/* D92: Secret ramen-engines (the optional engines' credentials) exists when any engine runs */}}
{{- define "ramen.engines.enabled" -}}
{{- if or (has (.Values.otel.collector | default "none") (list "phoenix" "jaeger")) (eq (.Values.evals.engine | default "none") "promptfoo") }}1{{ end -}}
{{- end -}}
{{- define "ramen.engines.secretName" -}}
{{ default "ramen-engines" .Values.engines.existingSecret }}
{{- end -}}
{{- define "ramen.redis.secretName" -}}
{{ default "ramen-redis" .Values.redis.auth.existingSecret }}
{{- end -}}
{{- define "ramen.builder.registry" -}}
{{- if .Values.builder.registry -}}{{ .Values.builder.registry }}
{{- else -}}{{ include "ramen.platform" . }}
{{- end -}}
{{- end -}}
{{/* where pods inside the cluster reach the console (Service console, port 443, TLS since D86): wake, judge, /otlp */}}
{{- define "ramen.internalUrl" -}}
{{ printf "https://console.%s.svc.cluster.local:443" .Release.Namespace }}
{{- end -}}
{{/* D86 §39.2: the chart services the console issues leaves for (Secret ramen-tls-<svc>) */}}
{{- define "ramen.tlsServices" -}}
{{- $s := list -}}
{{- if .Values.redis.enabled }}{{ $s = append $s "redis" }}{{ end -}}
{{- if has (.Values.otel.collector | default "none") (list "phoenix" "jaeger") }}{{ $s = append $s .Values.otel.collector }}{{ end -}}
{{- if eq (.Values.evals.engine | default "none") "promptfoo" }}{{ $s = append $s "promptfoo" }}{{ end -}}
{{- range (.Values.tls.extraServices | default list) }}{{ $s = append $s . }}{{ end -}}
{{ join "," $s }}
{{- end -}}
{{/* a PVC's class: the component's own (dict own root), else storage.className, else none (the cluster default) */}}
{{- define "ramen.storageClassName" -}}
{{- .own | default (.root.Values.storage | default dict).className -}}
{{- end -}}
