{{- define "shop.name" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "shop.selectorLabels" -}}
app.kubernetes.io/name: {{ include "shop.name" . }}
{{- end -}}

{{- define "shop.labels" -}}
{{ include "shop.selectorLabels" . }}
app.kubernetes.io/part-of: shop
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "shop.isWeb" -}}
{{- if eq .Values.role "web" -}}true{{- end -}}
{{- end -}}
