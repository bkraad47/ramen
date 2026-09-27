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
{{ printf "%s:%s" (default (printf "%s-docker.pkg.dev/%s/ramen/console" .Values.region .Values.project) .Values.image.repository) (.Values.image.tag | toString) }}
{{- end -}}
{{- define "ramen.gsa" -}}
{{ default (printf "ramen-console@%s.iam.gserviceaccount.com" .Values.project) .Values.serviceAccount.gsa }}
{{- end -}}
{{- define "ramen.secretName" -}}
{{ default "ramen-console" .Values.console.existingSecret }}
{{- end -}}
