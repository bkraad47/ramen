{{- define "ramen.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "ramen.fullname" -}}{{ printf "%s-%s" .Release.Name .Values.group | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "ramen.labels" -}}
app.kubernetes.io/name: {{ include "ramen.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
ramen.io/group: {{ .Values.group }}
ramen.io/environment: {{ .Values.environment }}
{{- end -}}
{{- define "ramen.workerName" -}}{{ printf "%s-worker-%s" (include "ramen.fullname" .root) .zone.name | trunc 63 | trimSuffix "-" }}{{- end -}}
