{{- define "satellite.labels" -}}
app.kubernetes.io/name: ramen-satellite
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
ramen.io/region: {{ required "region is required" .Values.region }}
{{- end -}}
{{/* the console's subject on this API server: gcp = GSA email (User), azure = workload identity object id (User), aws = group */}}
{{- define "satellite.identity" -}}
{{- if eq .Values.provider "gcp" -}}
{{ default (printf "ramen-console@%s.iam.gserviceaccount.com" (required "project is required on gcp" .Values.project)) .Values.console.identity }}
{{- else if eq .Values.provider "azure" -}}
{{ required "console.identity (object id of the console's identity) is required on azure" .Values.console.identity }}
{{- end -}}
{{- end -}}
{{- define "satellite.staticIpName" -}}
{{ default (printf "ramen-%s" .Values.region) .Values.gateway.staticIpName }}
{{- end -}}
