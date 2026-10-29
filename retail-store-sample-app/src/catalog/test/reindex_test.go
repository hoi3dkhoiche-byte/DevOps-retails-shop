package test

import (
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"

    "github.com/aws-containers/retail-store-sample-app/catalog/config"
    "github.com/aws-containers/retail-store-sample-app/catalog/model"
    "github.com/aws-containers/retail-store-sample-app/catalog/repository"
)

func TestReindexUsesSQLAndRejectsPartialBulkFailure(t *testing.T) {
    source, err := repository.NewRepository(dbConfig)
    if err != nil { t.Fatal(err) }
    db := source.(*repository.Database).DB
    product := model.Product{ID: "sql-only-reindex-test", Name: "SQL authoritative test product", Description: "Not in seed JSON", Price: 42}
    if err := db.Create(&product).Error; err != nil { t.Fatal(err) }
    defer db.Delete(&product)
    var bulk string
    failBulk := false
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.Header().Set("Content-Type", "application/json")
        switch {
        case r.Method == http.MethodHead:
            w.WriteHeader(http.StatusNotFound)
        case r.URL.Path == "/_bulk":
            body, _ := io.ReadAll(r.Body)
            bulk = string(body)
            if failBulk { io.WriteString(w, `{"errors":true}`) } else { io.WriteString(w, `{"errors":false}`) }
        default:
            io.WriteString(w, `{"version":{"number":"3.5.0","distribution":"opensearch"},"acknowledged":true}`)
        }
    }))
    defer server.Close()
    search, err := repository.NewOpenSearchRepository(config.OpenSearchConfiguration{Endpoint: server.URL, IndexName: "ci-products"}, source)
    if err != nil { t.Fatal(err) }
    if err := search.Reindex(); err != nil { t.Fatal(err) }
    if !strings.Contains(bulk, "SQL authoritative test product") { t.Fatal("reindex did not use SQL product rows") }
    failBulk = true
    if err := search.Reindex(); err == nil { t.Fatal("partial bulk indexing failures must fail reindex") }
}
