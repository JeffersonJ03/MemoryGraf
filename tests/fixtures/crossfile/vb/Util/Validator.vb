Namespace App.Util
    Module Validator
        Function IsValid(ByVal s As String) As Boolean
            Return s IsNot Nothing AndAlso s.Length > 0
        End Function
    End Module
End Namespace
