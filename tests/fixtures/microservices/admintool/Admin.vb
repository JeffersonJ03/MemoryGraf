' Admin tool (Visual Basic) — consume /api/billing.
Module Admin
    Const BillingRoute As String = "/api/billing"

    Function FormatId(ByVal id As Integer) As String
        Return "id-" & id
    End Function

    Function Fetch(ByVal id As Integer) As String
        Return FormatId(id) & " " & BillingRoute
    End Function
End Module
