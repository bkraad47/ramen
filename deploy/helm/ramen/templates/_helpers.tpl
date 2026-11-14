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
{{- define "ramen.image" -}}
{{- if eq .Values.provider "azure" -}}
{{ printf "%s:%s" (default (printf "%s/ramen/console" .Values.azure.acr) .Values.image.repository) (.Values.image.tag | toString) }}
{{- else if eq .Values.provider "aws" -}}
{{ printf "%s:%s" (default (printf "%s.dkr.ecr.%s.amazonaws.com/ramen/console" (.Values.aws.account | toString) .Values.region) .Values.image.repository) (.Values.image.tag | toString) }}
{{- else -}}
{{ printf "%s:%s" (default (printf "%s-docker.pkg.dev/%s/ramen/console" .Values.region .Values.project) .Values.image.repository) (.Values.image.tag | toString) }}
{{- end -}}
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

{{/* §24.2: "true" when a builder is configured — builder.enabled when set, else whether the provider's identity is given. */}}
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
{{- define "ramen.builder.registry" -}}
{{- if .Values.builder.registry -}}{{ .Values.builder.registry }}
{{- else if eq .Values.provider "azure" -}}{{ printf "%s/ramen" .Values.azure.acr }}
{{- else if eq .Values.provider "aws" -}}{{ printf "%s.dkr.ecr.%s.amazonaws.com/ramen" (.Values.aws.account | toString) .Values.region }}
{{- else -}}{{ printf "%s-docker.pkg.dev/%s/ramen" .Values.region .Values.project }}
{{- end -}}
{{- end -}}
{{/* where pods inside the cluster reach the console (Service console, port 80): the activator's wake call, §24.1 */}}
{{- define "ramen.internalUrl" -}}
{{ printf "http://console.%s.svc.cluster.local:80" .Release.Namespace }}
{{- end -}}
